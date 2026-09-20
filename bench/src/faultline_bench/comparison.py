from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from faultline_contracts import Fingerprint

Arm = Literal["elastic", "observe", "probe", "clone_probe"]
ARMS: dict[str, str] = {
    "elastic": "Elastic Agent Builder · read-only RCA",
    "observe": "Faultline · Observe",
    "probe": "Faultline · Probe",
    "clone_probe": "Faultline · Clone + Probe",
}
SCHEMA_VERSION = "faultline-comparison/1"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Protocol(Model):
    name: str = "Development-set comparison; diagnosis and reversible mitigation only"
    window_s: int = Field(default=5, ge=5, le=5)
    baseline_s: int = Field(default=130, ge=120, le=300)
    horizon_s: int = Field(default=300, ge=120, le=900)
    detection_s: int = Field(default=60, ge=5, le=120)
    recovery_s: int = Field(default=60, ge=5, le=120)
    review_s: int = Field(default=30, ge=15, le=120)
    action_budget: int = Field(default=5, ge=1, le=5)
    model: str = "gpt-4.1"
    clone_budget: int = Field(default=3, ge=1, le=5)
    notes: list[str] = Field(default_factory=lambda: [
        "Tuned development cases, not a held-out accuracy benchmark.",
        "Independent reset runs; shared 60-second sustained-SLO detection gate.",
        "Elapsed time starts at controller injection; healthy controls use the same scheduled origin.",
        "Recovery requires a complete 60-second healthy interval, baseline-compatible errors and maintained request rate.",
        "Recovery may be mitigation-supported, not a durable code fix or post-TTL recovery.",
        "Customer counts are C1 rate-integral estimates, not exact request counts; rejected requests and cost are unavailable.",
        "Elastic is a configured Agent Builder RCA baseline with a run-scoped read-only ES|QL tool, not all Elastic capabilities.",
        "The clone arm gates on reproduction; the current Product planner does not re-rank probes using clone measurements.",
        "No Devin patches, canaries, or Datadog execution in this comparison.",
    ])

    @model_validator(mode="after")
    def aligned(self):
        if any(value % self.window_s for value in (self.baseline_s, self.horizon_s, self.detection_s, self.recovery_s, self.review_s)):
            raise ValueError("protocol durations must align to C1 windows")
        return self


class Case(Model):
    id: str
    label: str
    world: Literal["storm", "degraded", "healthy"]
    expected: str
    workload_rps: int = Field(default=80, ge=1)
    params: dict[str, float] = Field(default_factory=dict)


def development_cases(include_healthy: bool = False) -> list[Case]:
    cases = [
        Case(id="case-a", label="Retry storm", world="storm", expected="H_meta", params={"delay_ms": 800, "duration_s": 20}),
        Case(id="case-b", label="Degraded database", world="degraded", expected="H_db", params={"capacity_qps": 40}),
    ]
    if include_healthy:
        cases.append(Case(id="case-c", label="Healthy control", world="healthy", expected="no_incident"))
    return cases


class Sample(Model):
    start_s: float
    end_s: float
    metrics: dict[str, float] = Field(default_factory=dict)
    slo_breached: bool | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.end_s <= self.start_s:
            raise ValueError("sample boundaries must increase")
        return self


class Event(Model):
    id: str = Field(default_factory=lambda: uuid4().hex)
    at_s: float
    kind: str
    title: str
    detail: str = ""
    environment: Literal["production", "clone", "observer"] = "observer"
    evidence: Literal["observed", "inferred", "measured", "operator"] = "observed"
    reference: str | None = None
    diagnosis: str | None = None
    action_id: str | None = None
    ttl_s: float | None = None


MAX_DETAIL = 4000


def _round(value):
    return round(value, 3) if isinstance(value, float) else value


def audit_detail(payload: dict) -> str:
    """Readable detail for one C4 audit payload, shown in the replay's event panel.

    Without this the Faultline arms render as bare one-line titles while an
    observer arm shows its whole answer, which understates the side that did
    the work. Everything here is copied from the run's own audit payload; the
    replay panel renders it as prose, so no JSON blobs.
    """
    lines: list[str] = []
    triage = payload.get("triage")
    if isinstance(triage, dict):
        if triage.get("reasoning"):
            lines.append(str(triage["reasoning"]))
        for hypothesis in triage.get("hypotheses", []):
            lines.append(f"{hypothesis.get('id')} — {hypothesis.get('label')}: {hypothesis.get('description')}")
        for prediction in triage.get("predictions", []):
            during = ", ".join(f"{m['metric']} {m['direction']}" for m in prediction.get("during", []))
            after = ", ".join(f"{m['metric']} {m['direction']}" for m in prediction.get("after_release", []))
            confirms = prediction.get("confirms_if")
            confirm_text = (f"confirms if {confirms['phase']} {confirms['metric']} is {confirms['expect']}"
                            if confirms else "diagnostic only, cannot confirm")
            lines.append(f"{prediction.get('hypothesis_id')} under {prediction.get('experiment_id')}: "
                         f"while held {during or 'no prediction'}; after release {after or 'no prediction'}; {confirm_text}")
    for candidate in payload.get("candidates", []) if isinstance(payload.get("candidates"), list) else []:
        lines.append(f"{candidate.get('experiment_id')}: separation {candidate.get('separation')}, "
                     f"blast radius {candidate.get('blast_radius_pct')}%, score {_round(candidate.get('score'))}"
                     + (" — selected" if candidate.get("selected") else ""))
    for support in payload.get("support", []) if isinstance(payload.get("support"), list) else []:
        lines.append(f"support {support.get('hypothesis_id')}: {_round(support.get('support'))}"
                     + ("" if support.get("confirmed") is None else f", confirmed={support['confirmed']}"))
    for observation in payload.get("observations", []) if isinstance(payload.get("observations"), list) else []:
        lines.append(f"{observation.get('phase')} {observation.get('metric')}: "
                     f"baseline {_round(observation.get('baseline'))} → measured {_round(observation.get('measured'))} "
                     f"(z {_round(observation.get('z'))}, {observation.get('direction')})")
    # "planner"/"investigation" only route the event to the right renderer upstream.
    skip = {"triage", "candidates", "support", "observations", "planner", "investigation"}
    scalars = [f"{key}: {_round(value)}" for key, value in payload.items()
               if key not in skip and isinstance(value, (str, int, float, bool))]
    if scalars:
        lines.append("; ".join(scalars))
    return "\n".join(lines)[:MAX_DETAIL]


class Metrics(Model):
    diagnosis: str | None = None
    correct: bool | None = None
    detection_s: float | None = None
    first_correct_s: float | None = None
    recovery_s: float | None = None
    recovery_status: Literal["recovered", "not_recovered", "not_applicable", "unknown"] = "unknown"
    failed_checkouts_estimate: float | None = None
    successful_checkouts_estimate: float | None = None
    sample_coverage_pct: float = 0
    production_actions: int = 0
    clone_actions: int = 0
    rollback_failures: int = 0
    tokens: int | None = None
    cost_usd: float | None = None


class Run(Model):
    id: str = Field(default_factory=lambda: uuid4().hex)
    case_id: str
    arm: Arm
    source: Literal["live", "synthetic"] = "live"
    status: Literal["not_run", "running", "completed", "error", "timeout", "blocked"] = "not_run"
    started_at: datetime | None = None
    duration_s: float = 0
    samples: list[Sample] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    metrics: Metrics | None = None
    provenance: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class Recording(Model):
    schema_version: Literal["faultline-comparison/1"] = SCHEMA_VERSION
    id: str = Field(default_factory=lambda: "cmp-" + uuid4().hex, pattern=r"^cmp-[A-Za-z0-9_-]{1,100}$")
    title: str = "Faultline comparison lab"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    protocol: Protocol = Field(default_factory=Protocol)
    cases: list[Case]
    runs: list[Run]

    @model_validator(mode="after")
    def identities(self):
        case_ids = {case.id for case in self.cases}
        if len(case_ids) != len(self.cases) or len({run.id for run in self.runs}) != len(self.runs):
            raise ValueError("duplicate case or run id")
        if any(run.case_id not in case_ids for run in self.runs):
            raise ValueError("unknown case id")
        return self


def new_recording(arms: list[Arm], protocol: Protocol, include_healthy: bool = False) -> Recording:
    cases = development_cases(include_healthy)
    digest = hashlib.sha256(json.dumps(protocol.model_dump(), sort_keys=True).encode()).hexdigest()[:16]
    return Recording(protocol=protocol, cases=cases, runs=[
        Run(case_id=case.id, arm=arm, provenance={"protocol_hash": digest, "model": protocol.model})
        for case in cases for arm in dict.fromkeys(arms)
    ])


def sample_from_fingerprint(fp: Fingerprint, origin: datetime) -> Sample:
    slos = [slo for slo in fp.slos if slo.value is not None]
    return Sample(
        start_s=(fp.window_start - origin).total_seconds(),
        end_s=(fp.window_end - origin).total_seconds(),
        metrics=fp.metrics(),
        slo_breached=any(slo.breached for slo in slos) if slos else None,
    )


def canonical_samples(samples: list[Sample], window_s: int = 5) -> list[Sample]:
    ordered = sorted(samples, key=lambda sample: (sample.start_s, sample.end_s))
    accepted: list[Sample] = []
    for sample in ordered:
        if not math.isclose(sample.end_s - sample.start_s, window_s, abs_tol=0.01):
            continue
        if accepted and sample.start_s < accepted[-1].end_s - 0.01:
            continue
        accepted.append(sample)
    return accepted


def sustained_start(samples: list[Sample], duration_s: int, predicate) -> float | None:
    start = previous_end = None
    for sample in samples:
        if not predicate(sample):
            start = previous_end = None
            continue
        if previous_end is None or not math.isclose(sample.start_s, previous_end, abs_tol=0.01):
            start = sample.start_s
        previous_end = sample.end_s
        if sample.end_s - start >= duration_s - 0.01:
            return start
    return None


def detection_time(samples: list[Sample], protocol: Protocol) -> float | None:
    start = sustained_start(canonical_samples(samples), protocol.detection_s, lambda sample: sample.slo_breached is True)
    return None if start is None else start + protocol.detection_s


def _bounds(samples: list[Sample], metric: str) -> tuple[float, float] | None:
    values = [sample.metrics[metric] for sample in samples if metric in sample.metrics]
    if len(values) < 6:
        return None
    typical = mean(values)
    return typical, max(pstdev(values), abs(typical) * 0.1)


def score_run(run: Run, case: Case, protocol: Protocol) -> Metrics:
    samples = canonical_samples(run.samples, protocol.window_s)
    baseline = [sample for sample in samples if sample.end_s <= 0 and sample.slo_breached is False]
    incident = [sample for sample in samples if sample.start_s >= 0 and sample.end_s <= protocol.horizon_s]
    coverage = min(100.0, len(incident) * protocol.window_s / protocol.horizon_s * 100)
    valid_events = sorted((event for event in run.events if 0 <= event.at_s <= protocol.horizon_s), key=lambda event: event.at_s)
    conclusions = [event for event in valid_events if event.kind == "conclusion" and event.diagnosis is not None]
    diagnosis = conclusions[-1].diagnosis if conclusions else None
    detected = next((event.at_s for event in valid_events if event.kind == "detect"), None)
    eligible = run.status not in ("not_run", "blocked", "running") and run.source == "live"
    correct = diagnosis == case.expected if eligible else None
    if case.world == "healthy" and detected is None and not conclusions and coverage >= 100 and run.status == "completed":
        diagnosis = "no_incident"
        correct = True if run.source == "live" else None
    first_correct = next((event.at_s for event in conclusions if event.diagnosis == case.expected), None)
    if first_correct is None and diagnosis == "no_incident" and correct:
        first_correct = float(protocol.horizon_s)
    actions = {event.action_id or event.id for event in valid_events if event.kind == "action_start" and event.environment == "production"}
    clone_actions = {event.action_id or event.id for event in valid_events if event.kind == "action_start" and event.environment == "clone"}
    error_bounds = _bounds(baseline, "svc.gateway.error_rate")
    load_bounds = _bounds(baseline, "svc.gateway.qps")
    recovery = None
    recovery_status = "unknown"
    if run.arm in ("elastic", "observe") or case.world == "healthy":
        recovery_status = "not_applicable"
    elif detected is not None and error_bounds is not None and load_bounds is not None:
        error_limit = min(1.0, error_bounds[0] + 3 * error_bounds[1])
        load_minimum = max(0.0, load_bounds[0] - 3 * load_bounds[1])
        def healthy(sample):
            errors = sample.metrics.get("svc.gateway.error_rate")
            load = sample.metrics.get("svc.gateway.qps")
            return (sample.start_s >= detected and sample.slo_breached is False and errors is not None
                    and load is not None and load > 0 and load >= load_minimum and errors <= error_limit)
        recovery = sustained_start(incident, protocol.recovery_s, healthy)
        recovery_observed = coverage >= 100 and all(
            sample.slo_breached is not None and "svc.gateway.error_rate" in sample.metrics and "svc.gateway.qps" in sample.metrics
            for sample in incident
        )
        recovery_status = "recovered" if recovery is not None else "not_recovered" if recovery_observed else "unknown"
    complete_counts = coverage >= 100 and all(
        "svc.gateway.qps" in sample.metrics and "svc.gateway.error_rate" in sample.metrics for sample in incident
    )
    failed = successful = None
    if complete_counts:
        failed = sum(sample.metrics["svc.gateway.qps"] * sample.metrics["svc.gateway.error_rate"] * protocol.window_s for sample in incident)
        successful = sum(sample.metrics["svc.gateway.qps"] * (1 - sample.metrics["svc.gateway.error_rate"]) * protocol.window_s for sample in incident)
    return Metrics(
        diagnosis=diagnosis, correct=correct, detection_s=detected, first_correct_s=first_correct if eligible else None,
        recovery_s=recovery, recovery_status=recovery_status, failed_checkouts_estimate=failed,
        successful_checkouts_estimate=successful, sample_coverage_pct=coverage,
        production_actions=len(actions), clone_actions=len(clone_actions),
        rollback_failures=sum(event.kind == "rollback_failed" for event in run.events if event.at_s >= 0),
        tokens=run.metrics.tokens if run.metrics else None,
    )


def save_recording(recording: Recording, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (recording.id + ".json")
    temporary = target.with_suffix(".tmp")
    temporary.write_text(recording.model_dump_json(indent=2))
    temporary.replace(target)
    return target


def example_recording() -> Recording:
    protocol = Protocol(horizon_s=120)
    recording = new_recording(["elastic", "observe", "probe", "clone_probe"], protocol)
    recording.id = "cmp-example"
    recording.title = "Illustrative playback · not benchmark results"
    for run in recording.runs:
        run.source = "synthetic"
        run.status = "completed" if run.arm in ("observe", "probe", "clone_probe") else "not_run"
        run.duration_s = 120
        run.warnings = ["Synthetic UI example. No vendor was run and no comparative result is implied."]
        if run.arm == "elastic":
            continue
        run.samples = [Sample(start_s=t, end_s=t + 5, metrics={
            "svc.gateway.p99_ms": 1300 + (t % 20) * 10,
            "svc.gateway.error_rate": 0.65,
            "svc.gateway.qps": 80,
            "svc.orders.retry_ratio": 3.7,
            "db.query_p99_ms": 900,
        }, slo_breached=True) for t in range(0, 120, 5) if t not in (40, 45)]
        run.events = [
            Event(at_s=5, kind="observation", title="Example telemetry arrives", detail="Sample values demonstrate the playback controls, not a measured incident."),
            Event(at_s=60, kind="detect", title="Example alert", detail="All playback panels share the same elapsed-time cursor."),
            Event(at_s=75, kind="hypothesis", title="Example investigation step", detail="An actual recording would show the agent's evidence and revisions here.", evidence="inferred"),
        ]
        if run.arm != "observe":
            run.events += [
                Event(at_s=85, kind="action_start", title="Example bounded probe", environment="production", action_id="example-action", ttl_s=30),
                Event(at_s=105, kind="action_end", title="Example probe released", environment="production", action_id="example-action"),
            ]
        if run.arm == "clone_probe":
            run.events.append(Event(at_s=70, kind="clone", title="Example isolated clone", environment="clone", detail="Clone activity is separate from production."))
    return recording
