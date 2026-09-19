"""C4 — Audit events. The audit log is also the source of truth for experiment phase
boundaries: experiment_start = lever applied, experiment_end = lever released.
Elasticsearch index: AUDIT_INDEX.
"""

import json
from datetime import datetime
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import Field

from .common import SCHEMA_VERSION, Model, utcnow

AUDIT_INDEX = "faultline-audit"


class Stage(IntEnum):
    ingest = 1
    detect = 2
    triage = 3
    experiment = 4
    mitigate = 5
    patch = 6
    canary = 7
    report = 8


class EventKind(str, Enum):
    detect = "detect"
    triage = "triage"
    experiment_start = "experiment_start"
    experiment_end = "experiment_end"
    action_apply = "action_apply"
    action_undo = "action_undo"
    refused = "refused"  # safety check rejected a proposed action
    verdict = "verdict"
    mitigation = "mitigation"
    patch_opened = "patch_opened"
    canary_update = "canary_update"
    report = "report"
    page_human = "page_human"


class Actor(str, Enum):
    llm = "llm"
    math = "math"
    adapter = "adapter"
    orchestrator = "orchestrator"
    human = "human"


class AuditEvent(Model):
    schema_version: str = SCHEMA_VERSION
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    incident_id: str
    ts: datetime = Field(default_factory=utcnow)
    stage: Stage
    kind: EventKind
    actor: Actor
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    action_id: str | None = None
    experiment_id: str | None = None


class ExperimentWindow(Model):
    experiment_id: str
    start: datetime  # lever applied
    release: datetime | None = None  # lever undone; None while still running


def experiment_windows(events: list[AuditEvent]) -> list[ExperimentWindow]:
    """Phase boundaries per experiment, from experiment_start/experiment_end events."""
    wins: dict[str, ExperimentWindow] = {}
    for e in sorted(events, key=lambda e: e.ts):
        if e.experiment_id is None:
            continue
        if e.kind == EventKind.experiment_start:
            wins[e.experiment_id] = ExperimentWindow(experiment_id=e.experiment_id, start=e.ts)
        elif e.kind == EventKind.experiment_end and e.experiment_id in wins:
            wins[e.experiment_id].release = e.ts
    return list(wins.values())


@runtime_checkable
class AuditSink(Protocol):
    def write(self, event: AuditEvent) -> None: ...

    def query(self, incident_id: str) -> list[AuditEvent]: ...


class JsonlSink:
    """Local file sink; the ES sink (Owner 2) implements the same protocol."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: AuditEvent) -> None:
        with self.path.open("a") as f:
            f.write(event.model_dump_json() + "\n")

    def query(self, incident_id: str) -> list[AuditEvent]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                ev = AuditEvent.model_validate(json.loads(line))
                if ev.incident_id == incident_id:
                    out.append(ev)
        return sorted(out, key=lambda e: e.ts)
