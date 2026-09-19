"""C6 investigator evidence is measured through public clone interfaces only."""

import json
from datetime import UTC, datetime
from pathlib import Path

from faultline_contracts import (
    ActionStatus,
    CloneEndpoints,
    CloneInfo,
    CloneSpec,
    CloneStatus,
    Experiment,
    Fingerprint,
    LabActionHandle,
    TriageResult,
)

from faultline_brain.investigator import (
    CloneInvestigator,
    CloneProbe,
    LabExperiment,
    score_clone_prediction,
)

FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "fixtures"


def _series():
    return [Fingerprint.model_validate(item) for item in json.loads((FIXTURES / "series_storm_experiment.json").read_text())]


class FakeCloneLab:
    def __init__(self):
        self.events = []
        self.clone = None

    def create(self, spec):
        self.events.append("create")
        self.clone = CloneInfo(
            clone_id="clone-a",
            status=CloneStatus.ready,
            spec=spec,
            created_at=datetime(2026, 9, 19, tzinfo=UTC),
            endpoints=CloneEndpoints(
                gateway_url="http://clone", control_url="http://clone:9901", stats_urls={}
            ),
        )
        return self.clone

    def apply(self, clone_id, action, params, ttl_s):
        self.events.append(f"apply:{action}")
        return LabActionHandle(
            action_id="action-a", clone_id=clone_id, action=action, params=params, ttl_s=ttl_s
        )

    def undo(self, handle):
        self.events.append(f"undo:{handle.action}")
        return handle.model_copy(update={"status": ActionStatus.undone})

    def reset(self, clone_id):
        self.events.append(f"reset:{clone_id}")
        return self.clone

    def destroy(self, clone_id):
        self.events.append(f"destroy:{clone_id}")
        return self.clone.model_copy(update={"status": CloneStatus.destroyed})


def test_investigator_reproduces_recovers_and_cleans_up_clone():
    series = _series()
    healthy, incident = series[0], series[12]
    observed = iter([incident, healthy])
    lab = FakeCloneLab()
    waits = []
    investigator = CloneInvestigator(lab, lambda _clone: next(observed), waits.append)

    evidence = investigator.investigate(
        "H_meta",
        CloneSpec(name="h-meta"),
        incident,
        healthy,
        LabExperiment("db_latency", {"extra_ms": 800}, 20, observe_after_s=30, recovery_wait_s=5),
    )

    assert evidence.reproduction.reproduced is True
    assert evidence.recovery.recovered is True
    assert evidence.survives_falsification is True
    assert waits == [30, 5]
    assert evidence.reproduction.action.recipe() == {
        "action": "db_latency", "params": {"extra_ms": 800}, "ttl_s": 20
    }
    assert lab.events == ["create", "apply:db_latency", "undo:db_latency", "reset:clone-a", "destroy:clone-a"]


def test_clone_probe_scores_measured_c2_predictions():
    series = _series()
    triage = TriageResult.model_validate_json((FIXTURES / "triage_hero.json").read_text())
    experiments = json.loads((FIXTURES / "experiments.json").read_text())
    retry_cap = next(item for item in experiments if item["id"] == "retry_cap_0_20s")
    probe = CloneProbe(
        healthy_baseline=series[:12],
        incident_baseline=series[12:24],
        during=series[24:28],
        after_release=series[28:],
    )

    evidence = score_clone_prediction(
        triage, "H_meta", Experiment.model_validate(retry_cap), probe
    )

    assert evidence.measured_expectations == 5
    assert evidence.matched_expectations == 5
    assert evidence.predicts is True
