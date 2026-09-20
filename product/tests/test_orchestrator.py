import pytest
from faultline_contracts import ActionStatus, Actor, EventKind, HypothesisSupport, JsonlSink, LeverError, Stage
from faultline_product.adapters import (
    FixtureBrain,
    FixtureCanaryDeployer,
    FixtureClock,
    FixtureDevinAdapter,
    FixtureLeverAdapter,
)
from faultline_product.fixtures import load_fixture
from faultline_product.orchestrator import BudgetExceeded, Orchestrator
from faultline_product.ports import CanaryTarget
from faultline_product.renderer import TerminalRenderer


def _orchestrator(tmp_path, *, experiment=None, telemetry=None, budget=5, levers=None, output=None):
    bundle = load_fixture("storm")
    telemetry = telemetry or bundle.telemetry
    clock = FixtureClock(bundle.experiment_start, bundle.telemetry.last_window_end)
    brain = FixtureBrain(bundle.triage, experiment or bundle.experiment, bundle.verdict)
    audit = JsonlSink(tmp_path / "audit.jsonl")
    levers = levers or FixtureLeverAdapter(clock=clock)
    output = output if output is not None else []
    return (
        Orchestrator(
            levers,
            audit,
            FixtureDevinAdapter(),
            FixtureCanaryDeployer(),
            TerminalRenderer(output.append),
            telemetry,
            brain,
            clock,
            clock.sleep,
            budget,
        ),
        audit,
        bundle,
    )


def test_refuses_large_blast_radius(tmp_path):
    bundle = load_fixture("storm")
    experiment = bundle.experiment.model_copy(update={"blast_radius_pct": 51})
    orchestrator, audit, _ = _orchestrator(tmp_path, experiment=experiment)
    result = orchestrator.run("refused", bundle.experiment_start)
    assert result.diagnosis == "refused"
    assert any(event.kind == EventKind.refused for event in audit.query("refused"))


def test_budget_exceeded_pages_human(tmp_path):
    bundle = load_fixture("storm")
    orchestrator, audit, _ = _orchestrator(tmp_path, budget=0)
    with pytest.raises(BudgetExceeded):
        orchestrator.run("budget", bundle.experiment_start)
    assert any(event.kind == EventKind.page_human for event in audit.query("budget"))


def test_kept_mitigation_has_mitigate_action_id(tmp_path):
    orchestrator, audit, bundle = _orchestrator(tmp_path)
    orchestrator.run("kept", bundle.experiment_start)
    events = audit.query("kept")
    assert any(
        event.stage == Stage.mitigate and event.kind == EventKind.action_apply and event.action_id
        for event in events
    )
    assert any(
        event.stage == Stage.mitigate and event.kind == EventKind.action_undo and event.action_id
        for event in events
    )


class BreachedTelemetry:
    def __init__(self, source):
        self.source = source

    def window(self, start, end):
        return self.source.first_breach()

    def series(self, start, end, step_s=5):
        return [self.source.first_breach()]


def test_canary_regression_auto_undoes_and_refuses(tmp_path):
    bundle = load_fixture("storm")
    orchestrator, audit, _ = _orchestrator(tmp_path, telemetry=BreachedTelemetry(bundle.telemetry))
    result = orchestrator.run("regression", bundle.experiment_start)
    events = audit.query("regression")
    assert any(
        event.kind == EventKind.action_undo and event.stage == Stage.canary for event in events
    )
    assert any(event.kind == EventKind.refused and event.stage == Stage.canary for event in events)
    assert result.canary.status.value == "regressed"
    assert events[-1].summary == "incident escalated: canary regressed"


def test_canary_version_evidence_fails_closed():
    bundle = load_fixture("storm")
    healthy = next(
        fp
        for fp in bundle.telemetry.series(
            bundle.experiment_start, bundle.telemetry.last_window_end
        )
        if not any(slo.breached for slo in fp.slos)
    )
    target = CanaryTarget("patch", "v2", "abc123", service_name="orders_v2")

    assert Orchestrator._canary_regression([healthy], target) == "missing orders_v2 telemetry"

    v1 = healthy.services["orders"]
    failing_v2 = v1.model_copy(update={"error_rate": (v1.error_rate or 0) + 0.1})
    with_v2 = healthy.model_copy(
        update={"services": {**healthy.services, "orders_v2": failing_v2}}
    )
    assert (
        Orchestrator._canary_regression([with_v2], target)
        == "orders-v2 error rate exceeds orders-v1"
    )

    # a v2 that never errored has no error counter yet: honest None, not a regression
    clean_v2 = v1.model_copy(update={"error_rate": None})
    with_clean_v2 = healthy.model_copy(
        update={"services": {**healthy.services, "orders_v2": clean_v2}}
    )
    assert Orchestrator._canary_regression([with_clean_v2], target) is None

    idle_v2 = v1.model_copy(update={"qps": 0.0})
    with_idle_v2 = healthy.model_copy(
        update={"services": {**healthy.services, "orders_v2": idle_v2}}
    )
    assert (
        Orchestrator._canary_regression([with_idle_v2], target)
        == "orders_v2 served no traffic during canary"
    )


class CanaryRefusingLevers:
    def __init__(self, delegate):
        self.delegate = delegate

    def catalog(self):
        return self.delegate.catalog()

    def estimate_blast_radius(self, lever_id, params):
        return self.delegate.estimate_blast_radius(lever_id, params)

    def apply(self, lever_id, params, ttl_s):
        if lever_id == "canary_weight":
            raise LeverError("orders-v2 is not running")
        return self.delegate.apply(lever_id, params, ttl_s)

    def undo(self, handle):
        return self.delegate.undo(handle)

    def status(self, handle):
        return self.delegate.status(handle)


def test_canary_refusal_finishes_run_and_pages_human(tmp_path):
    output = []
    levers = CanaryRefusingLevers(FixtureLeverAdapter())
    orchestrator, audit, bundle = _orchestrator(tmp_path, levers=levers, output=output)

    result = orchestrator.run("canary-refused", bundle.experiment_start)

    assert result.patch is not None
    events = audit.query("canary-refused")
    canary_events = [event for event in events if event.stage == Stage.canary]
    assert [event.kind for event in canary_events] == [EventKind.refused, EventKind.page_human]
    assert not any(event.kind == EventKind.action_apply for event in canary_events)
    assert events[-1].kind == EventKind.report
    assert events[-1].summary == "incident escalated: canary refused"
    assert result.canary.status.value == "refused"
    assert "[canary] canary_weight refused: orders-v2 is not running — paged human" in output
    assert output[-1].startswith("[report] escalated:")


class NoExperimentBrain(FixtureBrain):
    def plan(self, triage, catalog, blast_radius):
        del triage, catalog, blast_radius


def test_no_separating_experiment_finishes_run_and_pages_human(tmp_path):
    orchestrator, audit, bundle = _orchestrator(tmp_path)
    orchestrator._brain = NoExperimentBrain(bundle.triage, bundle.experiment, bundle.verdict)

    result = orchestrator.run("no-experiment", bundle.experiment_start)

    assert result.diagnosis == "refused"
    events = audit.query("no-experiment")
    assert [event.kind for event in events if event.stage == Stage.experiment] == [
        EventKind.triage,  # planner scored the candidates, none selected
        EventKind.refused,
        EventKind.page_human,
    ]
    assert events[-1].kind == EventKind.report


class FollowUpFixtureBrain(FixtureBrain):
    def __init__(self, triage, experiment, verdicts, follow_up):
        super().__init__(triage, experiment, verdicts[-1])
        self._verdicts = iter(verdicts)
        self._follow_up = follow_up

    def judge(self, triage, experiment, baseline, during, after_release):
        del triage, experiment, baseline, during, after_release
        return next(self._verdicts).model_copy(update={"incident_id": self._incident_id})

    def confirmation_experiment(self, triage, hypothesis_id, catalog, blast_radius, excluded_ids):
        del triage, catalog, blast_radius
        assert hypothesis_id == "H_db"
        if excluded_ids:  # the follow-up lookup excludes the probe already run
            assert "retry_cap_0_20s" in excluded_ids
        return self._follow_up  # also the relief lever held as mitigation afterwards


def test_unconfirmed_diagnostic_probe_runs_direct_confirmation_follow_up(tmp_path):
    orchestrator, audit, bundle = _orchestrator(tmp_path)
    db_failover = next(item for item in bundle.experiments if item.id == "db_failover_30s")
    first = bundle.verdict.model_copy(
        update={
            "diagnosis": "none_of_the_above",
            "confirmed": False,
            "support": [HypothesisSupport(hypothesis_id="H_db", support=1.0, confirmed=False)],
        }
    )
    second = bundle.verdict.model_copy(
        update={
            "diagnosis": "H_db",
            "confirmed": True,
            "support": [HypothesisSupport(hypothesis_id="H_db", support=1.0, confirmed=True)],
        }
    )
    orchestrator._brain = FollowUpFixtureBrain(
        bundle.triage, bundle.experiment, [first, second], db_failover
    )

    result = orchestrator.run("follow-up", bundle.experiment_start)

    events = audit.query("follow-up")
    starts = [event.experiment_id for event in events if event.kind == EventKind.experiment_start]
    assert starts == ["retry_cap_0_20s", "db_failover_30s"]
    assert result.diagnosis == "H_db"
    # H_db is not cured by the probe: the relieving lever (failover) is held as the mitigation
    held = [e for e in events if e.kind == EventKind.mitigation and "held as mitigation" in e.summary]
    assert held and held[0].payload["lever_id"] == "db_failover"


def test_relief_mitigation_is_kept_through_the_canary(tmp_path):
    """A failover held for H_db is not released before the canary: the patch does not replace it."""
    orchestrator, audit, bundle = _orchestrator(tmp_path)
    db_failover = next(item for item in bundle.experiments if item.id == "db_failover_30s")
    first = bundle.verdict.model_copy(update={
        "diagnosis": "none_of_the_above", "confirmed": False,
        "support": [HypothesisSupport(hypothesis_id="H_db", support=1.0, confirmed=False)]})
    second = bundle.verdict.model_copy(update={
        "diagnosis": "H_db", "confirmed": True,
        "support": [HypothesisSupport(hypothesis_id="H_db", support=1.0, confirmed=True)]})
    orchestrator._brain = FollowUpFixtureBrain(bundle.triage, bundle.experiment, [first, second], db_failover)

    orchestrator.run("relief", bundle.experiment_start)

    events = audit.query("relief")
    held = next(e for e in events if e.kind == EventKind.mitigation and "held as mitigation" in e.summary)
    undone_ids = {e.action_id for e in events if e.kind == EventKind.action_undo}
    assert held.action_id not in undone_ids  # failover still in place when the report is written
    assert not any("released emergency mitigation" in e.summary for e in events)
    # and the report never claims "ready" while a relief lever is what keeps production up
    report = events[-1]
    assert report.kind == EventKind.report and report.summary != "incident report ready"
    if report.payload["canary_status"] == "passed":
        assert report.summary == "incident mitigated; human action required"
        assert report.payload["mitigation_held"] == "db_failover"
        assert any(e.kind == EventKind.page_human and "needs a human fix" in e.summary for e in events)


class StickyUndoLevers(FixtureLeverAdapter):
    """undo() reports `undone` but the target keeps the lever active (the DELETE never landed)."""

    def __init__(self, clock=None):
        super().__init__(clock=clock) if clock else super().__init__()
        self.undo_calls = []

    def undo(self, handle):
        self.undo_calls.append(handle.action_id)
        return handle.model_copy(update={"status": ActionStatus.undone})

    def status(self, handle):
        return ActionStatus.active


def test_release_that_does_not_land_is_retried_recorded_and_paged(tmp_path):
    output = []
    levers = StickyUndoLevers()
    orchestrator, audit, bundle = _orchestrator(tmp_path, levers=levers, output=output)

    result = orchestrator.run("sticky", bundle.experiment_start)

    events = audit.query("sticky")
    applied = [e.action_id for e in events if e.kind == EventKind.action_apply]
    # every release was retried exactly once
    assert all(levers.undo_calls.count(action_id) == 2 for action_id in applied)
    for stage, lever in ((Stage.experiment, "retry_cap"), (Stage.mitigate, "retry_cap"), (Stage.canary, "canary_weight")):
        refused = [e for e in events if e.stage == stage and e.kind == EventKind.refused and e.payload.get("release_failed")]
        assert refused and refused[0].actor == Actor.adapter and refused[0].action_id
        assert refused[0].summary.startswith(f"release of {lever} did not land; TTL ")
        assert refused[0].summary.endswith("s will revert it")
        assert any(e.stage == stage and e.kind == EventKind.page_human and e.action_id == refused[0].action_id for e in events)
    # the audit never claims a release that did not happen
    assert all(e.payload["status"] == "active" for e in events if e.kind == EventKind.action_undo)
    assert result.canary.status.value == "regressed"
    assert "did not land" in result.canary.detail
    assert events[-1].summary == "incident escalated: canary regressed"
    assert any(line.startswith("[experiment] release of retry_cap did not land") for line in output)


def test_planner_candidate_table_is_audited(tmp_path):
    orchestrator, audit, bundle = _orchestrator(tmp_path)
    orchestrator.run("planner", bundle.experiment_start)

    planner = [e for e in audit.query("planner") if e.stage == Stage.experiment and e.payload.get("planner")]
    assert len(planner) == 1
    event = planner[0]
    assert event.kind == EventKind.triage and event.actor == Actor.math
    assert event.summary == "planner: 1 candidates scored"
    assert event.experiment_id == bundle.experiment.id
    (row,) = event.payload["candidates"]
    assert row == {
        "experiment_id": bundle.experiment.id, "lever_id": "retry_cap", "separation": 1,
        "score": 1 - 0.1 * bundle.experiment.blast_radius_pct,
        "blast_radius_pct": bundle.experiment.blast_radius_pct, "selected": True,
    }


def test_triage_event_carries_the_full_triage_result(tmp_path):
    orchestrator, audit, bundle = _orchestrator(tmp_path)
    orchestrator.run("triage-dump", bundle.experiment_start)

    triage = next(e for e in audit.query("triage-dump") if e.stage == Stage.triage and e.kind == EventKind.triage)
    assert triage.actor == Actor.llm
    assert triage.payload["hypotheses"] == [h.id for h in bundle.triage.hypotheses]
    dump = triage.payload["triage"]
    assert dump["incident_id"] == "triage-dump"
    assert [h["id"] for h in dump["hypotheses"]] == triage.payload["hypotheses"]
    assert {p["experiment_id"] for p in dump["predictions"]} == {p.experiment_id for p in bundle.triage.predictions}
    assert all("confirms_if" in p for p in dump["predictions"])
