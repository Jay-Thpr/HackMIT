from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from faultline_contracts import ActionHandle, ActionStatus, Fingerprint, utcnow
from faultline_contracts.clone import CloneStatus, HttpCloneLab
from faultline_contracts.fault import DegradeDbFault, HttpFaultController, StormFault, World
from faultline_product.adapters.live_telemetry import LiveTelemetrySource, fingerprint_from_snapshots
from faultline_product.adapters.sandbox import SandboxLeverAdapter

from .comparison import ARMS, Event, Metrics, Protocol, Recording, detection_time, example_recording, new_recording, sample_from_fingerprint, save_recording, score_run
from .comparison_agents import ElasticBaseline
from .comparison_runtime import EventLog

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "runs" / "comparisons"


def source_digest():
    digest = hashlib.sha256()
    for relative in ("bench/src", "product/src", "faultline/brain/src", "faultline/telemetry/src", "contracts/src"):
        for path in sorted((ROOT / relative).rglob("*.py")):
            digest.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


class WindowSampler:
    def __init__(self, source):
        self.source = source
        self.snapshots = deque(maxlen=2600)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._poll, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=12)

    def _poll(self):
        while not self.stop_event.is_set():
            try:
                snapshot = self.source.snapshot()
                with self.lock:
                    self.snapshots.append((datetime.fromtimestamp(snapshot["orders"]["t"], tz=timezone.utc), snapshot))
            except Exception:
                pass
            self.stop_event.wait(0.5)

    def window(self, start, end):
        with self.lock:
            entries = list(self.snapshots)
        before = [item for item in entries if item[0] <= start]
        after = [item for item in entries if item[0] <= end]
        if not before or not after:
            return None
        previous, current = before[-1], after[-1]
        if (start - previous[0]).total_seconds() > 1.5 or (end - current[0]).total_seconds() > 1.5:
            return None
        if current[0] <= previous[0]:
            return None
        try:
            return fingerprint_from_snapshots(previous[1], current[1], start, end)
        except (KeyError, TypeError, ValueError):
            return None


def write_observations(path: Path, windows: list[Fingerprint]):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps([fp.model_dump(mode="json") for fp in windows], allow_nan=False))
    temporary.replace(path)


def read_events(path: Path) -> list[Event]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(Event.model_validate_json(line))
        except ValueError:
            continue
    return events


def refresh_run(run, directory):
    run.events = sorted(read_events(directory / "events.jsonl"), key=lambda event: event.at_s)
    usage_path = directory / "usage.jsonl"
    if usage_path.exists():
        values = []
        for line in usage_path.read_text().splitlines():
            try:
                values.append(json.loads(line)["tokens"])
            except (ValueError, KeyError):
                continue
        if values:
            run.metrics = Metrics(tokens=sum(values))


def worker_command(run, directory, origin, detected_at, args):
    return [sys.executable, "-m", "faultline_bench.comparison_runtime", "--arm", run.arm,
            "--directory", str(directory), "--origin", origin.isoformat(), "--detected-at", detected_at.isoformat(),
            "--run-id", run.id, "--control-url", args.control_url, "--lab-url", args.lab_url]


def stop_worker(process):
    if process is None or process.poll() is not None:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def cleanup_owned(run, directory, args, log):
    ok = True
    handles_path = directory / "handles.jsonl"
    if handles_path.exists():
        adapter = SandboxLeverAdapter(args.control_url)
        handles = [ActionHandle.model_validate_json(line) for line in handles_path.read_text().splitlines() if line.strip()]
        for handle in reversed(handles):
            try:
                result = adapter.undo(handle)
                if result.status not in (ActionStatus.undone, ActionStatus.expired):
                    raise RuntimeError("undo not confirmed")
                log.emit("cleanup", "Production lever released at end of observation", environment="production", action_id=handle.action_id)
            except Exception:
                ok = False
                log.emit("rollback_failed", "End-of-run rollback failed", environment="production", action_id=handle.action_id)
    if run.arm == "clone_probe":
        lab = HttpCloneLab(args.lab_url, timeout_s=150)
        try:
            for clone in lab.list():
                if clone.spec.name.startswith("cmp-" + run.id[:16] + "-") and clone.status != CloneStatus.destroyed:
                    result = lab.destroy(clone.clone_id)
                    if result.status != CloneStatus.destroyed:
                        raise RuntimeError("clone teardown not confirmed")
                    log.emit("cleanup", "Owned clone removed", environment="clone", reference=clone.clone_id)
        except Exception:
            ok = False
            log.emit("rollback_failed", "Owned clone cleanup could not be confirmed", environment="clone")
    return ok


def run_case(recording, run, case, args, controller):
    protocol = recording.protocol
    directory = args.output / ".work" / run.id
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "protocol.json").write_text(protocol.model_dump_json())
    sampler = WindowSampler(LiveTelemetrySource(
        orders_url=args.orders_url, payments_url=args.payments_url, loadgen_url=args.loadgen_url,
    ))
    process = None
    origin = utcnow() + timedelta(seconds=protocol.baseline_s + 2)
    log = EventLog(directory, origin)
    windows = []
    run.started_at = origin
    run.status = "running"
    run.provenance.update({"reasoning_provider": "elastic-agent-builder" if run.arm == "elastic" else "openai",
                           "scope": "C1 telemetry; no hidden controller state; no patches or canaries"})
    if run.arm == "clone_probe":
        run.warnings.append("Product clone investigator may use its labelled seeded-recipe fallback; clone tokens are not fully metered.")
    sampler.start()
    next_start = origin - timedelta(seconds=protocol.baseline_s)
    injected = False
    detected = False
    cleanup_ok = True
    try:
        while utcnow() < origin + timedelta(seconds=protocol.horizon_s + 1):
            now = utcnow()
            while next_start + timedelta(seconds=5) <= now:
                end = next_start + timedelta(seconds=5)
                if end > origin + timedelta(seconds=protocol.horizon_s):
                    break
                fp = sampler.window(next_start, end)
                if fp is not None:
                    windows.append(fp)
                    run.samples.append(sample_from_fingerprint(fp, origin))
                next_start = end
                write_observations(directory / "telemetry.json", windows)
                refresh_run(run, directory)
                run.duration_s = min(protocol.horizon_s, max(0, (now - origin).total_seconds()))
                save_recording(recording, args.output)
            if not injected and now >= origin:
                baseline = [fp for fp in windows if fp.window_end <= origin]
                if len(baseline) != protocol.baseline_s // 5 or any(not fp.slos or any(slo.value is None or slo.breached for slo in fp.slos) for fp in baseline):
                    run.status = "blocked"
                    run.warnings.append("A complete healthy baseline was not recorded; no fault injected.")
                    break
                injected = True
                if case.world == "storm":
                    controller.storm(StormFault(delay_ms=int(case.params["delay_ms"]), duration_s=int(case.params["duration_s"])))
                elif case.world == "degraded":
                    controller.degrade_db(DegradeDbFault(capacity_qps=case.params["capacity_qps"]))
                log.emit("observation", "Comparison observation window started", evidence="operator")
            declared = detection_time([sample for sample in run.samples if sample.start_s >= 0], protocol)
            if injected and not detected and declared is not None:
                detected = True
                log.emit("detect", "Sustained checkout SLO breach", evidence="measured")
                detected_at = windows[-1].window_end
                process = subprocess.Popen(worker_command(run, directory, origin, detected_at, args), cwd=ROOT / "bench", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.2)
        if run.status == "running":
            stop_worker(process)
            result_path = directory / "worker-result.json"
            result = json.loads(result_path.read_text()) if result_path.exists() else None
            run.status = result["status"] if result else "timeout" if detected else "completed"
            if result and result.get("error"):
                run.warnings.append("Responder error: " + (result.get("detail") or result["error"]))
            if not detected and case.world != "healthy":
                run.warnings.append("No sustained incident detected; retained as an unsuccessful development case, not silently excluded.")
            run.duration_s = float(protocol.horizon_s)
    except BaseException as exc:
        run.status = "error"
        run.warnings.append("Run interrupted: " + type(exc).__name__)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        stop_worker(process)
        sampler.stop()
        try:
            cleanup_ok = cleanup_owned(run, directory, args, log)
        except Exception:
            cleanup_ok = False
            run.warnings.append("Could not finish owned-resource cleanup.")
        if injected:
            try:
                controller.reset()
            except Exception:
                cleanup_ok = False
                run.warnings.append("Controller reset failed; stop and inspect the sandbox.")
        refresh_run(run, directory)
        if not cleanup_ok:
            run.warnings.append("Cleanup incomplete; inspect the owned resources before any further run.")
        run.duration_s = max(run.duration_s, max((event.at_s for event in run.events), default=0))
        run.metrics = score_run(run, case, protocol)
        if run.arm == "clone_probe" and run.metrics.tokens is not None:
            run.provenance["triage_tokens_only"] = str(run.metrics.tokens)
            run.metrics.tokens = None
        save_recording(recording, args.output)
    return cleanup_ok


def validate_local_endpoint(url):
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("comparison targets must be explicit loopback HTTP sandbox endpoints")
    _ = parsed.port


@contextmanager
def target_lock(control_url):
    parsed = urlsplit(control_url)
    identity = hashlib.sha256(f"loopback:{parsed.port or 80}".encode()).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / ("faultline-comparison-" + identity + ".lock")
    with path.open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another comparison owns this target") from None
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def preflight(args, recording):
    for name in ("fault_url", "control_url", "orders_url", "payments_url", "loadgen_url", "lab_url"):
        validate_local_endpoint(getattr(args, name))
    required = set()
    if any(run.arm != "elastic" for run in recording.runs):
        required.add("OPENAI_API_KEY")
    if any(run.arm == "elastic" for run in recording.runs):
        required.update(("KIBANA_URL", "FAULTLINE_ELASTICSEARCH_URL", "ELASTIC_AGENT_BUILDER_API_KEY", "FAULTLINE_ELASTICSEARCH_API_KEY"))
    missing = sorted(name for name in required if not os.environ.get(name))
    if missing:
        raise ValueError("missing configuration: " + ", ".join(missing))
    with httpx.Client(timeout=10, trust_env=False) as client:
        for url in (args.orders_url, args.payments_url, args.loadgen_url):
            client.get(url + "/stats").raise_for_status()
        levers = client.get(args.control_url + "/admin/levers")
        levers.raise_for_status()
        if any(value.get("active") for value in levers.json().values()):
            raise RuntimeError("sandbox has active levers; do not interrupt its responder")
        if any(run.arm == "clone_probe" for run in recording.runs):
            client.get(args.lab_url + "/healthz").raise_for_status()
        initial = client.get(args.loadgen_url + "/stats").json().get("gauges", {}).get("target_rps")
        if not isinstance(initial, (int, float)) or isinstance(initial, bool):
            raise RuntimeError("cannot record original workload for restoration")
    return initial


def main(argv=None):
    parser = argparse.ArgumentParser(description="Small development-set responder comparison. Defaults to an offline plan; no live actions without explicit flags.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="run paid model calls and reversible C5/C3/C6 actions")
    mode.add_argument("--demo", action="store_true", help="write an explicitly synthetic playback example, no network")
    mode.add_argument("--plan", action="store_true", help="print the offline plan (default)")
    parser.add_argument("--exclusive-sandbox", action="store_true", help="confirm this sandbox is reserved: no other watch/demo/benchmark may run")
    parser.add_argument("--allow-elastic-setup", action="store_true", help="authorize creation of the comparison-only ES index, run-scoped Kibana tool and stored conversations")
    parser.add_argument("--arms", nargs="+", choices=list(ARMS), default=list(ARMS))
    parser.add_argument("--include-healthy", action="store_true")
    parser.add_argument("--cases", nargs="+", choices=("case-a", "case-b", "case-c"), help="run just these development cases; case-c also requires --include-healthy")
    parser.add_argument("--horizon-s", type=int, default=300)
    parser.add_argument("--model", default="gpt-4.1")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--title", default="Faultline development comparison")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    for name, default in (("fault", 9900), ("control", 9901), ("orders", 8101), ("payments", 8102), ("loadgen", 8103), ("lab", 9910)):
        parser.add_argument("--" + name + "-url", default=f"http://127.0.0.1:{default}")
    args = parser.parse_args(argv)
    if args.demo:
        print(save_recording(example_recording(), args.output))
        return 0
    protocol = Protocol(horizon_s=args.horizon_s, model=args.model)
    recording = new_recording(args.arms, protocol, args.include_healthy)
    if args.cases:
        if set(args.cases) - {case.id for case in recording.cases}:
            parser.error("case-c requires --include-healthy")
        recording.cases = [case for case in recording.cases if case.id in args.cases]
        recording.runs = [run for run in recording.runs if run.case_id in args.cases]
    recording.title = args.title
    rng = random.Random(args.seed)
    rng.shuffle(recording.runs)
    for run in recording.runs:
        run.provenance["order_seed"] = str(args.seed)
    if not args.execute:
        print(recording.model_dump_json(indent=2))
        return 0
    if not args.exclusive_sandbox:
        parser.error("--execute requires --exclusive-sandbox; this will reset and inject faults into the target")
    if "elastic" in args.arms and not args.allow_elastic_setup:
        parser.error("Elastic requires --allow-elastic-setup; cloud setup and model calls are not read-only operations")
    try:
        with target_lock(args.control_url):
            initial_rps = preflight(args, recording)
            controller = HttpFaultController(args.fault_url, timeout_s=150)
            state = controller.state()
            if state.world != World.none or state.active:
                raise RuntimeError("sandbox already has a fault; refusing to reset unrelated work")
            revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
            frozen_digest = source_digest()
            for run in recording.runs:
                run.provenance["git_sha"] = revision
                run.provenance["source_sha256"] = frozen_digest
            save_recording(recording, args.output)
            try:
                for run in recording.runs:
                    if source_digest() != frozen_digest:
                        raise RuntimeError("comparison source changed during the frozen pilot; stopping before the next case")
                    case = next(case for case in recording.cases if case.id == run.case_id)
                    if run.arm == "elastic":
                        elastic = ElasticBaseline(
                            os.environ["KIBANA_URL"], os.environ["FAULTLINE_ELASTICSEARCH_URL"],
                            os.environ["ELASTIC_AGENT_BUILDER_API_KEY"], os.environ["FAULTLINE_ELASTICSEARCH_API_KEY"],
                            os.environ.get("FAULTLINE_AGENT_BUILDER_INFERENCE_ID", "faultline-openai-investigation"),
                        )
                        try:
                            elastic.prepare(run.id, protocol.model)
                            run.provenance["telemetry_tool"] = elastic.tool["id"]
                            run.provenance["setup"] = "Cloud tool provisioned before baseline; ingestion/model execution counted after detection."
                        except Exception:
                            run.status = "blocked"
                            run.warnings.append("Elastic setup failed before injection; no substitute provider was used.")
                            save_recording(recording, args.output)
                            raise
                        finally:
                            elastic.close()
                    with httpx.Client(timeout=10, trust_env=False) as client:
                        client.post(args.fault_url + "/debug/load", json={"rps": case.workload_rps}).raise_for_status()
                    print(f"Running {case.id} / {ARMS[run.arm]}", flush=True)
                    if not run_case(recording, run, case, args, controller):
                        raise RuntimeError("cleanup incomplete; no further cases will run")
                    print(f"  {run.status}; result saved", flush=True)
            finally:
                with httpx.Client(timeout=10, trust_env=False) as client:
                    client.post(args.fault_url + "/debug/load", json={"rps": initial_rps}).raise_for_status()
            print(save_recording(recording, args.output))
            return 0 if all(run.status == "completed" for run in recording.runs) else 1
    except Exception as exc:
        print("Comparison stopped: " + type(exc).__name__ + (": " + str(exc) if isinstance(exc, (ValueError, RuntimeError)) else ""), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
