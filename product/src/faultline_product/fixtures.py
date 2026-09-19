import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from faultline_contracts import (
    AuditEvent,
    EventKind,
    Experiment,
    Fingerprint,
    TriageResult,
    Verdict,
)

from .adapters.fixture import FixtureTelemetrySource
from .paths import CONTRACT_FIXTURES


@dataclass(frozen=True)
class FixtureBundle:
    triage: TriageResult
    experiment: Experiment
    experiments: list[Experiment]
    experiment_start: datetime
    verdict: Verdict
    telemetry: FixtureTelemetrySource


def load_fixture(name: str, root: Path = CONTRACT_FIXTURES) -> FixtureBundle:
    if name != "storm":
        raise ValueError(f"unsupported fixture {name!r}; available: storm")

    triage = TriageResult.model_validate(_read_json(root / "triage_hero.json"))
    verdict = Verdict.model_validate(_read_json(root / "verdict_storm.json"))
    experiments = [
        Experiment.model_validate(item) for item in _read_json(root / "experiments.json")
    ]
    experiment = next(item for item in experiments if item.id == "retry_cap_0_20s")
    audit_events = [
        AuditEvent.model_validate(item) for item in _read_jsonl(root / "audit_hero.jsonl")
    ]
    experiment_start = next(
        event.ts for event in audit_events if event.kind == EventKind.experiment_start
    )
    fingerprints = [
        Fingerprint.model_validate(item)
        for item in _read_json(root / "series_storm_experiment.json")
    ]
    return FixtureBundle(
        triage=triage,
        experiment=experiment,
        experiments=experiments,
        experiment_start=experiment_start,
        verdict=verdict,
        telemetry=FixtureTelemetrySource(fingerprints),
    )


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load fixture {path}: {exc}") from exc


def _read_jsonl(path: Path):
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line]
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load fixture {path}: {exc}") from exc
