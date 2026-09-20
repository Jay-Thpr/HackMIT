from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import socket
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import openai

from faultline_contracts import Fingerprint
from faultline_bench import comparison_agents as agents
from faultline_bench import comparison_runtime as runtime
from faultline_bench.comparison import Protocol

SCENARIOS = ("plain", "fenced", "preamble", "repair", "invalid_metric", "malformed", "provider_error", "late")
ARMS = ("observe", "elastic")
MISSING_METRIC = "replay.intentionally_absent_metric"
MAX_INPUT_BYTES = 20 * 1024 * 1024


class OfflineBoundaryError(RuntimeError):
    pass


class ReplayClock:
    def __init__(self, now: datetime):
        self.now = now

    def utcnow(self):
        return self.now

    def sleep(self, seconds):
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("invalid virtual-clock advance")
        self.now += timedelta(seconds=seconds)


def deny_external(*args, **kwargs):
    raise OfflineBoundaryError("offline replay forbids network, processes and infrastructure actions")


@contextmanager
def offline_boundary():
    with ExitStack() as stack:
        for name in ("connect", "connect_ex"):
            stack.enter_context(patch.object(socket.socket, name, deny_external))
        stack.enter_context(patch.object(socket, "create_connection", deny_external))
        stack.enter_context(patch.object(socket, "getaddrinfo", deny_external))
        stack.enter_context(patch.object(subprocess, "Popen", deny_external))
        stack.enter_context(patch.object(runtime, "run_probe", deny_external))
        stack.enter_context(patch.object(runtime, "HttpCloneLab", deny_external))
        stack.enter_context(patch.object(runtime, "SandboxLeverAdapter", deny_external))
        yield


def load_source(run_dir: Path, origin: datetime | None = None):
    telemetry_path = run_dir / "telemetry.json"
    with telemetry_path.open("rb") as file:
        raw = file.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("telemetry input exceeds 20 MB")
    payload = json.loads(raw)
    if not isinstance(payload, list) or not payload:
        raise ValueError("telemetry must contain a nonempty list of C1 windows")
    windows = sorted((Fingerprint.model_validate(item) for item in payload), key=lambda fp: fp.window_end)
    protocol = Protocol.model_validate_json((run_dir / "protocol.json").read_text())
    for fp in windows:
        if fp.window_start.utcoffset() != timedelta(0) or fp.window_end.utcoffset() != timedelta(0) or fp.window_end <= fp.window_start:
            raise ValueError("telemetry windows must have increasing UTC boundaries")
    if origin is None:
        origin = windows[0].window_start + timedelta(seconds=protocol.baseline_s)
    if origin.utcoffset() != timedelta(0):
        raise ValueError("replay origin must be UTC")
    return windows, protocol, origin, hashlib.sha256(raw).hexdigest()


def fake_source(destination: Path, world_name: str):
    from faultline_contracts.fakes import FakeWorld
    from faultline_contracts.fault import DegradeDbFault, StormFault

    if world_name not in ("storm", "degraded", "healthy"):
        raise ValueError("unknown fake world")
    protocol = Protocol(horizon_s=120)
    world = FakeWorld(seed=17, start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    world.advance(protocol.baseline_s)
    origin = world.now
    if world_name == "storm":
        world.storm(StormFault(delay_ms=800, duration_s=20))
    elif world_name == "degraded":
        world.degrade_db(DegradeDbFault(capacity_qps=40))
    world.advance(protocol.horizon_s)
    windows = world.series(world.start, world.now)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "telemetry.json").write_text(json.dumps([fp.model_dump(mode="json") for fp in windows]))
    (destination / "protocol.json").write_text(protocol.model_dump_json())
    return origin


class ProviderScript:
    def __init__(self, scenario, clock, deadline, run_id):
        self.scenario, self.clock, self.deadline, self.run_id = scenario, clock, deadline, run_id
        self.windows = []
        self.calls = []
        self.reads = []
        self.requests = []
        self.published = []

    def reply(self, request):
        known = sorted({key for fp in self.windows for key in fp.metrics()})
        evidence = ["db.qps"] if "db.qps" in known else known[:1]
        rendered = json.dumps(request)
        number = len(self.calls) + 1
        self.calls.append({
            "number": number, "at": self.clock.now.isoformat(),
            "window_count": len(self.windows),
            "latest_window_end": max((fp.window_end.isoformat() for fp in self.windows), default=None),
            "feedback_mentions_rejected_key": MISSING_METRIC in rendered,
            "conversation_id": request.get("conversation_id"),
            "structured_output_requested": "response_format" in request,
            "request": request,
        })
        if self.scenario == "late":
            self.clock.sleep(max(0, (self.deadline - self.clock.now).total_seconds()) + 1)
        else:
            self.clock.sleep(0.25)
        if self.scenario == "provider_error":
            raise RuntimeError("scripted offline provider failure")
        if self.scenario == "malformed":
            return "{not valid JSON"
        if self.scenario == "invalid_metric" or (self.scenario == "repair" and number == 1):
            evidence = [MISSING_METRIC]
        payload = json.dumps({
            "diagnosis": "abstain", "summary": "Scripted offline assessment, not a vendor conclusion.",
            "evidence": evidence, "hypotheses": [], "recommendation": "No actions in offline replay.",
        })
        if self.scenario == "fenced":
            return "```json\n" + payload + "\n```"
        if self.scenario == "preamble":
            return "Here is the assessment:\n" + payload
        return payload

    def openai_factory(self, **kwargs):
        def create(**request):
            reply = self.reply(request)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))], usage=SimpleNamespace(total_tokens=10))
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: None)

    def elastic_transport(self, request):
        path = request.url.path
        self.requests.append({"method": request.method, "path": path})
        tool = agents.scoped_tool(self.run_id)
        if request.method == "GET" and path == "/api/agent_builder/tools/" + tool["id"]:
            return httpx.Response(200, json=tool)
        if request.method == "POST" and path == "/" + agents.INDEX + "/_bulk":
            rows = request.content.decode().splitlines()
            for index in range(1, len(rows), 2):
                document = json.loads(rows[index])
                if document.get("run_id") != self.run_id:
                    raise OfflineBoundaryError("fake transport refused cross-run evidence")
                evidence = json.loads(document["evidence"])
                end = datetime.fromisoformat(evidence["window_end"])
                if end > self.clock.now:
                    raise OfflineBoundaryError("future telemetry reached Elastic transport")
                self.published.append(evidence)
            return httpx.Response(200, json={"errors": False})
        if request.method == "POST" and path == "/api/agent_builder/converse":
            body = json.loads(request.content)
            expected = [{"tool_ids": [tool["id"]]}]
            if body.get("configuration_overrides", {}).get("tools") != expected:
                raise OfflineBoundaryError("fake transport refused broadened tools")
            reply = self.reply(body)
            return httpx.Response(200, json={
                "status": "completed", "conversation_id": "offline-conversation",
                "response": {"message": reply},
                "steps": [{"type": "tool_call", "tool_id": tool["id"], "results": [{"type": "esql", "data": self.published}]}],
            })
        raise OfflineBoundaryError("unrecognised offline transport route")


def implementation_digest():
    digest = hashlib.sha256()
    for module in (agents, runtime):
        digest.update(Path(module.__file__).read_bytes())
    return digest.hexdigest()


def replay_one(run_dir: Path, destination: Path, arm: str, scenario: str, *, origin=None, start_s=None):
    if arm not in ARMS or scenario not in SCENARIOS:
        raise ValueError("offline replay supports only observer arms and scripted scenarios")
    source_root, target = run_dir.resolve(), destination.resolve()
    if target == source_root or source_root in target.parents:
        raise ValueError("replay output must not modify the source run directory")
    windows, protocol, origin, input_hash = load_source(run_dir, origin)
    start_s = protocol.detection_s if start_s is None else start_s
    if not math.isfinite(start_s) or start_s < 0:
        raise ValueError("start-s must be finite and nonnegative")
    deadline = min(origin + timedelta(seconds=protocol.horizon_s), windows[-1].window_end)
    clock = ReplayClock(origin + timedelta(seconds=start_s))
    if clock.now >= deadline or not any(fp.window_end <= clock.now for fp in windows):
        raise ValueError("no recorded observer interval available at the requested start")
    protocol = protocol.model_copy(update={"horizon_s": int((deadline - origin).total_seconds())})
    protocol = Protocol.model_validate(protocol.model_dump())
    target.mkdir(parents=True, exist_ok=False)
    (target / "telemetry.json").write_text(json.dumps([fp.model_dump(mode="json") for fp in windows]))
    (target / "protocol.json").write_text(protocol.model_dump_json())
    run_id = uuid4().hex
    script = ProviderScript(scenario, clock, deadline, run_id)
    real_file_telemetry = runtime.FileTelemetry
    real_elastic = agents.ElasticBaseline
    source_before = implementation_digest()

    class ReplayTelemetry(real_file_telemetry):
        def all(self):
            available = [fp for fp in super().all() if fp.window_end <= clock.now]
            script.windows = available
            script.reads.append({"at": clock.now.isoformat(), "window_count": len(available),
                                 "latest_window_end": available[-1].window_end.isoformat() if available else None})
            return available

    def elastic_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(script.elastic_transport)
        return real_elastic(*args, **kwargs)

    environment = {
        "OPENAI_API_KEY": "offline-only", "KIBANA_URL": "https://kibana.replay.invalid",
        "FAULTLINE_ELASTICSEARCH_URL": "https://es.replay.invalid",
        "ELASTIC_AGENT_BUILDER_API_KEY": "offline-only", "FAULTLINE_ELASTICSEARCH_API_KEY": "offline-only",
        "FAULTLINE_AGENT_BUILDER_INFERENCE_ID": "offline-inference",
    }
    with offline_boundary(), patch.dict(os.environ, environment), \
            patch.object(runtime, "utcnow", clock.utcnow), \
            patch.object(runtime, "time", SimpleNamespace(sleep=clock.sleep)), \
            patch.object(runtime, "FileTelemetry", ReplayTelemetry), \
            patch.object(runtime, "ElasticBaseline", elastic_factory), \
            patch.object(openai, "OpenAI", script.openai_factory):
        exit_code = runtime.worker_main([
            "--arm", arm, "--directory", str(target), "--origin", origin.isoformat(),
            "--detected-at", clock.now.isoformat(), "--run-id", run_id,
            "--control-url", "http://offline.invalid:1", "--lab-url", "http://offline.invalid:2",
        ])
    result = json.loads((target / "worker-result.json").read_text())
    events_path = target / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.exists() else []
    expected = "timeout" if scenario == "late" else "error" if scenario in ("invalid_metric", "malformed", "provider_error") else "completed"
    checks = {
        "expected_worker_status": result.get("status") == expected,
        "implementation_unchanged": implementation_digest() == source_before,
        "provider_was_called": bool(script.calls),
        "no_future_windows": all(not read["latest_window_end"] or read["latest_window_end"] <= read["at"] for read in script.reads),
    }
    if scenario == "repair":
        checks["one_immediate_corrective_reask"] = len(script.calls) >= 2 and (
            datetime.fromisoformat(script.calls[1]["at"]) - datetime.fromisoformat(script.calls[0]["at"])
        ).total_seconds() < protocol.review_s and script.calls[1]["feedback_mentions_rejected_key"]
        if arm == "elastic":
            checks["repair_keeps_conversation"] = len(script.calls) >= 2 and script.calls[1]["conversation_id"] == "offline-conversation"
    if scenario in ("invalid_metric", "malformed"):
        checks["repair_budget_is_two_calls"] = len(script.calls) == 2
    if result.get("status") in ("error", "timeout"):
        checks["worker_error_has_detail"] = bool(result.get("detail"))
        checks["error_event_has_detail"] = any(event.get("kind") == "error" and event.get("detail") for event in events)
        checks["traceback_artifact_exists"] = (target / "traceback.txt").is_file()
    report = {
        "schema_version": "faultline-observer-replay/1", "source": "offline-fake-provider",
        "arm": arm, "scenario": scenario, "expected_status": expected, "worker_result": result,
        "exit_code": exit_code, "checks": checks, "passed": all(checks.values()),
        "source_telemetry_sha256": input_hash, "implementation_sha256": source_before,
        "origin": origin.isoformat(), "start_s": start_s, "virtual_end_s": (clock.now - origin).total_seconds(),
        "virtual_horizon_s": protocol.horizon_s, "provider_calls": len(script.calls),
        "telemetry_reads": script.reads, "artifact_directory": str(target),
        "note": "Observer/worker contract test with real recorded or FakeWorld windows and scripted replies. Not model accuracy, recovery, cost or live latency evidence.",
    }
    (target / "provider-calls.json").write_text(json.dumps(script.calls, indent=2))
    (target / "replay-report.json").write_text(json.dumps(report, indent=2))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline observer replay: real worker/parser paths, virtual time, fake provider transports; no network or infrastructure actions.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--run-dir", type=Path, help="recorded .work/<run-id> directory containing telemetry.json and protocol.json")
    source.add_argument("--fake-world", choices=("storm", "degraded", "healthy"))
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument("--origin", help="optional explicit UTC clock origin; default is first recorded window_start + baseline_s")
    parser.add_argument("--start-s", type=float, help="virtual observer start offset, default detection_s; this does not benchmark detection")
    parser.add_argument("--output", type=Path, default=Path(tempfile.gettempdir()) / "faultline-observer-replays")
    args = parser.parse_args(argv)
    output = args.output / ("replay-" + uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    origin = datetime.fromisoformat(args.origin.replace("Z", "+00:00")) if args.origin else None
    run_dir = args.run_dir
    if run_dir is None:
        run_dir = output / "source"
        origin = fake_source(run_dir, args.fake_world or "storm")
    reports = [replay_one(run_dir, output / f"{arm}-{scenario}", arm, scenario, origin=origin, start_s=args.start_s)
               for arm in dict.fromkeys(args.arms) for scenario in dict.fromkeys(args.scenarios)]
    summary = {"schema_version": "faultline-observer-replay-suite/1", "source": "offline-fake-provider", "reports": reports}
    (output / "replay-suite.json").write_text(json.dumps(summary, indent=2))
    for report in reports:
        failures = [name for name, ok in report["checks"].items() if not ok]
        print(f"{report['arm']:8} {report['scenario']:14} {'PASS' if report['passed'] else 'FAIL'} " + ", ".join(failures))
    print("Offline artifacts: " + str(output))
    return 0 if all(report["passed"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
