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

from types import SimpleNamespace

from faultline_brain.investigator import (
    CloneInvestigator,
    CloneProbe,
    LabExperiment,
    score_clone_prediction,
    similarity,
)
from faultline_brain.investigator_agent import (
    AgenticCloneInvestigator,
    InvestigatorAgent,
    InvestigatorValidationError,
    LabParams,
    LabProposal,
    MetricDirection,
)
from faultline_contracts.clone import LAB_CATALOG

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


def test_similarity_multiwindow_reference_and_absolute_floors():
    series = _series()
    healthy, incident = series[0], series[12]
    # healthy reference with error_rate ~0 vs an observed 2% error rate: the absolute
    # sigma floor (0.05) keeps that jitter from reading as a breach
    observed = healthy.model_copy(deep=True)
    observed.services["orders"].error_rate = 0.02

    sim = similarity([healthy, series[1], series[2]], observed)
    assert sim.matches and sim.matching_metrics == sim.shared_metrics
    assert sim.z_scores["svc.orders.error_rate"] < 3.0

    storm = similarity([healthy, series[1], series[2]], incident)
    assert not storm.matches

    # a single fingerprint reference still works
    assert similarity(healthy, observed).matches


def _proposal(action="db_latency", params=None, ttl_s=20, observe_after_s=30,
              predicted=(("db.qps", "up"),), stop=False):
    params = params or {"extra_ms": 800}
    return LabProposal(
        action=action,
        params=LabParams(
            extra_ms=params.get("extra_ms"), capacity_qps=params.get("capacity_qps"),
            service=params.get("service"), cpus=params.get("cpus"),
            max_retries=params.get("max_retries"), timeout_ms=params.get("timeout_ms"),
        ),
        ttl_s=ttl_s, observe_after_s=observe_after_s,
        predicted=[MetricDirection(metric=m, direction=d) for m, d in predicted],
        rationale="test", stop=stop, stop_reason="done" if stop else "",
    )


class ScriptedAgent:
    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.calls = []

    def propose(self, hypothesis, catalog, production_incident, healthy, history, attempts_left):
        self.calls.append(len(history))
        return self.proposals.pop(0)


class _Hyp:
    id = "H_meta"
    label = "meta"
    description = ""


def test_agentic_investigator_reproduces_on_second_attempt():
    series = _series()
    healthy, incident = series[0], series[12]
    observed = iter([healthy, incident, healthy])  # miss, reproduce, recover
    lab = FakeCloneLab()
    agent = ScriptedAgent([_proposal(), _proposal(params={"extra_ms": 900}, observe_after_s=35)])

    result = AgenticCloneInvestigator(lab, lambda _c: next(observed), agent, budget=3).investigate(
        _Hyp(), CloneSpec(name="h-meta"), incident, [healthy]
    )

    assert len(result.attempts) == 2
    assert result.attempts[0].reproduced is False
    assert result.attempts[1].reproduced is True
    assert result.evidence is not None and result.evidence.survives_falsification
    assert result.recipe == {"action": "db_latency", "params": {"extra_ms": 900}, "ttl_s": 20}
    # the clone was reset between attempts and destroyed at the end
    resets = [e for e in lab.events if e.startswith("reset")]
    assert len(resets) == 2  # one between attempts, one in cleanup
    assert lab.events[0] == "create" and lab.events[-1] == "destroy:clone-a"


def test_agentic_investigator_budget_exhausted():
    series = _series()
    healthy, incident = series[0], series[12]
    lab = FakeCloneLab()
    agent = ScriptedAgent([_proposal(), _proposal()])

    result = AgenticCloneInvestigator(
        lab, lambda _c: healthy, agent, budget=2
    ).investigate(_Hyp(), CloneSpec(name="h-meta"), incident, [healthy])

    assert len(result.attempts) == 2 and result.evidence is None and result.recipe is None
    assert lab.events[-1] == "destroy:clone-a"


class _FakeCompletions:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def create(self, model, messages, response_format):
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.payloads.pop(0)))],
            usage=None,
        )


def test_investigator_agent_retries_on_invalid_proposal():
    series = _series()
    healthy, incident = series[0], series[12]
    good = _proposal().model_dump_json()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(['{"action": "nope"}', good]))
    )
    agent = InvestigatorAgent(client)

    proposal = agent.propose(_Hyp(), LAB_CATALOG, incident, [healthy], [], 3)

    assert proposal.action == "db_latency"
    assert client.chat.completions.calls == 2


def test_investigator_agent_raises_after_repeated_invalid():
    series = _series()
    healthy, incident = series[0], series[12]
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(['{"action": "nope"}', '{"action": "nope"}']))
    )
    agent = InvestigatorAgent(client)

    try:
        agent.propose(_Hyp(), LAB_CATALOG, incident, [healthy], [], 3)
    except InvestigatorValidationError:
        pass
    else:
        raise AssertionError("expected InvestigatorValidationError")
