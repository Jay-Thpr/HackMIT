from __future__ import annotations

import argparse
import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from uuid import uuid4

from faultline_contracts import ActionHandle, ActionStatus, AuditEvent, EventKind, Fingerprint, JsonlSink, utcnow
from faultline_contracts.clone import HttpCloneLab, WorkloadSpec
from faultline_product.adapters.brain import LiveBrain
from faultline_product.adapters.investigate import LabInvestigation
from faultline_product.adapters.sandbox import SandboxLeverAdapter
from faultline_product.orchestrator import Orchestrator
from faultline_product.renderer import TerminalRenderer

from .comparison import Event, Protocol, audit_detail
from .comparison_agents import ElasticBaseline, openai_diagnose


class EventLog:
    def __init__(self, directory: Path, origin: datetime):
        self.directory, self.origin = directory, origin
        self._lock = threading.Lock()

    def emit(self, kind: str, title: str, **fields):
        event = Event(at_s=(utcnow() - self.origin).total_seconds(), kind=kind, title=title, **fields)
        with self._lock, (self.directory / "events.jsonl").open("a") as file:
            file.write(event.model_dump_json() + "\n")
        return event


class FileTelemetry:
    def __init__(self, path: Path):
        self.path = path

    def all(self) -> list[Fingerprint]:
        return [Fingerprint.model_validate(item) for item in json.loads(self.path.read_text())]

    def series(self, start: datetime, end: datetime, step_s: int = 5):
        return [fp for fp in self.all() if fp.window_start >= start and fp.window_end <= end]

    def window(self, start: datetime, end: datetime):
        windows = [fp for fp in self.all() if start <= fp.window_end <= end]
        if not windows:
            raise ValueError("no completed telemetry window")
        return windows[-1]


class RecordingAudit:
    def __init__(self, directory: Path, events: EventLog):
        self.sink = JsonlSink(directory / "audit.jsonl")
        self.events = events

    def write(self, event: AuditEvent):
        self.sink.write(event)
        if event.kind in (EventKind.detect, EventKind.action_apply, EventKind.action_undo):
            return
        fields = {"reference": "c4:" + event.event_id, "evidence": "measured" if event.actor.value == "math" else "inferred",
                  "detail": audit_detail(event.payload)}
        if event.kind == EventKind.verdict:
            self.events.emit("conclusion", event.summary, diagnosis=event.payload.get("diagnosis"), **fields)
        elif event.payload.get("investigation"):
            self.events.emit("clone_result", event.summary, environment="clone", **fields)
        else:
            title = "Investigation or action refused" if event.kind == EventKind.refused else event.summary
            self.events.emit(event.kind.value, title, **fields)

    def query(self, incident_id):
        return self.sink.query(incident_id)


class RecordingLevers:
    def __init__(self, inner, log: EventLog, deadline: datetime, *, environment="production", budget=5):
        self.inner, self.log, self.deadline = inner, log, deadline
        self.environment, self.budget, self.used = environment, budget, 0

    def catalog(self):
        return [spec for spec in self.inner.catalog() if spec.id != "canary_weight"]

    def estimate_blast_radius(self, lever_id, params):
        return self.inner.estimate_blast_radius(lever_id, params)

    def apply(self, lever_id, params, ttl_s):
        if lever_id == "canary_weight" or self.used >= self.budget or utcnow() >= self.deadline:
            raise RuntimeError("comparison action budget or deadline exceeded")
        self.used += 1
        handle = self.inner.apply(lever_id, params, ttl_s)
        if self.environment == "production":
            with (self.log.directory / "handles.jsonl").open("a") as file:
                file.write(handle.model_dump_json() + "\n")
        self.log.emit("action_start", lever_id, environment=self.environment, evidence="observed", action_id=handle.action_id, ttl_s=handle.ttl_s, detail=json.dumps(params))
        return handle

    def undo(self, handle):
        try:
            result = self.inner.undo(handle)
            if result.status not in (ActionStatus.undone, ActionStatus.expired):
                raise RuntimeError("rollback not confirmed")
        except Exception:
            self.log.emit("rollback_failed", "Rollback could not be confirmed", environment=self.environment, action_id=handle.action_id)
            raise
        self.log.emit("action_end", handle.lever_id + " released", environment=self.environment, action_id=handle.action_id)
        return result

    def status(self, handle):
        return self.inner.status(handle)


class RecordingLab:
    def __init__(self, inner, log: EventLog, run_id: str, deadline: datetime, rps: float):
        self.inner, self.log, self.run_id, self.deadline, self.rps = inner, log, run_id, deadline, rps

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def create(self, spec):
        if utcnow() >= self.deadline:
            raise RuntimeError("comparison deadline reached")
        spec = spec.model_copy(update={"name": "cmp-" + self.run_id[:16] + "-" + uuid4().hex[:8], "workload": WorkloadSpec(rps=self.rps)})
        self.log.emit("clone_start", "Creating clean clone", environment="clone", detail=spec.name)
        clone = self.inner.create(spec)
        self.log.emit("clone", "Clone ready", environment="clone", reference=clone.clone_id)
        return clone

    def apply(self, clone_id, action, params, ttl_s):
        if utcnow() >= self.deadline:
            raise RuntimeError("comparison deadline reached")
        handle = self.inner.apply(clone_id, action, params, ttl_s)
        self.log.emit("action_start", action, environment="clone", action_id=handle.action_id, ttl_s=ttl_s, detail=json.dumps(params), reference=clone_id)
        return handle

    def undo(self, handle):
        result = self.inner.undo(handle)
        self.log.emit("action_end", handle.action + " released", environment="clone", action_id=handle.action_id)
        return result

    def destroy(self, clone_id):
        result = self.inner.destroy(clone_id)
        self.log.emit("clone_end", "Clone teardown returned " + result.status.value, environment="clone", reference=clone_id)
        return result


def run_probe(arm, telemetry, directory, run_id, protocol, log, deadline, control_url, lab_url, detected_at):
    from openai import OpenAI
    from faultline_brain import InvestigatorAgent
    from faultline_product.fixtures import load_fixture

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=45, max_retries=0)
    def usage(item):
        if item.get("total_tokens") is not None:
            with (directory / "usage.jsonl").open("a") as file:
                file.write(json.dumps({"tokens": item["total_tokens"]}) + "\n")
    brain = LiveBrain(load_fixture("storm").experiments, client=client, model=protocol.model, triage_fallback=None, usage_sink=usage)
    levers = RecordingLevers(SandboxLeverAdapter(control_url), log, deadline, budget=protocol.action_budget)
    audit = RecordingAudit(directory, log)
    def bounded_sleep(seconds):
        if utcnow() + timedelta(seconds=seconds) > deadline:
            raise TimeoutError("comparison horizon reached")
        time.sleep(seconds)
    investigation = None
    if arm == "clone_probe":
        healthy = [fp for fp in telemetry.all() if fp.window_end <= log.origin and fp.services.get("gateway") and fp.services["gateway"].qps]
        if not healthy:
            raise ValueError("clone requires observed workload")
        lab = RecordingLab(HttpCloneLab(lab_url, timeout_s=120), log, run_id, deadline, mean(fp.services["gateway"].qps for fp in healthy))
        investigation = LabInvestigation(
            lab, max_clones=1, agent=InvestigatorAgent(client, model=protocol.model), budget=protocol.clone_budget,
            sleep=bounded_sleep, recipe_sink=lambda _: None,
            levers_factory=lambda clone: RecordingLevers(SandboxLeverAdapter(clone.endpoints.control_url, timeout_s=20), log, deadline, environment="clone", budget=protocol.clone_budget + 3),
        )
    orchestrator = Orchestrator(levers, audit, None, None, TerminalRenderer(write=lambda _: None), telemetry, brain,
                                sleep=bounded_sleep, action_budget=protocol.action_budget, investigation=investigation, investigation_gate=True)
    fp = orchestrator.detect(run_id, detected_at)
    triage = orchestrator.triage(run_id, fp)
    experiment = orchestrator.plan(run_id, triage)
    if experiment is None:
        log.emit("conclusion", "No separating experiment selected", diagnosis="abstain", evidence="inferred")
        return
    if arm == "clone_probe":
        results, triage = orchestrator.investigate(run_id, triage, fp, experiment, detected_at)
        if not results:
            raise RuntimeError("clone arm failed; refusing silent production-only fallback")
        if triage is None:
            log.emit("conclusion", "No hypothesis reproduced", diagnosis="abstain", evidence="measured")
            return
    baseline, during, after = orchestrator.experiment(run_id, experiment, detected_at)
    verdict = orchestrator.judge(run_id, triage, experiment, baseline, during, after)
    if not verdict.confirmed:
        follow_up = orchestrator.confirmation_experiment(run_id, triage, verdict, experiment)
        if follow_up is not None:
            experiment = follow_up
            baseline, during, after = orchestrator.experiment(run_id, experiment, detected_at)
            verdict = orchestrator.judge(run_id, triage, experiment, baseline, during, after)
    if verdict.confirmed:
        orchestrator.mitigate(run_id, triage, experiment, verdict)
    else:
        log.emit("escalation", "Diagnosis not confirmed; no mitigation applied")
    client.close()


def run_observer(arm, telemetry, directory, run_id, protocol, log, deadline):
    from openai import OpenAI

    previous = []
    elastic = None
    client = None
    try:
        if arm == "elastic":
            elastic = ElasticBaseline(
                os.environ["KIBANA_URL"], os.environ["FAULTLINE_ELASTICSEARCH_URL"],
                os.environ["ELASTIC_AGENT_BUILDER_API_KEY"], os.environ["FAULTLINE_ELASTICSEARCH_API_KEY"],
                os.environ.get("FAULTLINE_AGENT_BUILDER_INFERENCE_ID", "faultline-openai-investigation"),
            )
            elastic.attach(run_id)
            log.emit("setup", "Run-scoped Elastic telemetry tool verified", reference=elastic.tool["id"])
        else:
            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=45, max_retries=0)
        while utcnow() < deadline:
            windows = telemetry.all()
            if elastic:
                elastic.publish(run_id, windows)
                result, calls = elastic.diagnose(windows)
                for call in calls:
                    log.emit("tool", "Elastic read-only telemetry query", reference=call["tool_id"])
            else:
                log.emit("tool", "Read available C1 telemetry history")
                result, tokens = openai_diagnose(client, protocol.model, windows, previous)
                if tokens is not None:
                    with (directory / "usage.jsonl").open("a") as file:
                        file.write(json.dumps({"tokens": tokens}) + "\n")
            if utcnow() > deadline:
                raise TimeoutError("provider response arrived after comparison deadline")
            log.emit("conclusion", result.summary, diagnosis=result.diagnosis, evidence="inferred", detail=json.dumps(result.model_dump()))
            previous.append(result.model_dump())
            time.sleep(min(protocol.review_s, max(0, (deadline - utcnow()).total_seconds())))
    finally:
        if elastic:
            elastic.close()
        if client:
            client.close()


def worker_main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=("elastic", "observe", "probe", "clone_probe"), required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--detected-at", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--control-url", required=True)
    parser.add_argument("--lab-url", required=True)
    args = parser.parse_args(argv)
    protocol = Protocol.model_validate_json((args.directory / "protocol.json").read_text())
    origin = datetime.fromisoformat(args.origin)
    deadline = origin + timedelta(seconds=protocol.horizon_s)
    log = EventLog(args.directory, origin)
    telemetry = FileTelemetry(args.directory / "telemetry.json")
    try:
        if args.arm in ("probe", "clone_probe"):
            run_probe(args.arm, telemetry, args.directory, args.run_id, protocol, log, deadline, args.control_url, args.lab_url, datetime.fromisoformat(args.detected_at))
        else:
            run_observer(args.arm, telemetry, args.directory, args.run_id, protocol, log, deadline)
        result = {"status": "completed"}
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:500]
        log.emit("error", "Responder stopped: " + type(exc).__name__, detail=detail)
        (args.directory / "traceback.txt").write_text(traceback.format_exc())
        result = {"status": "timeout" if isinstance(exc, TimeoutError) else "error",
                  "error": type(exc).__name__, "detail": detail}
    (args.directory / "worker-result.json").write_text(json.dumps(result))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(worker_main())
