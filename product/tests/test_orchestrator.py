import pytest
from faultline_contracts import ActionStatus, Actor, AuditEvent, EventKind, HypothesisSupport, JsonlSink, LeverError, Stage
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


def _orchestrator(tmp_path, *, experiment=None, telemetry=None, budget=5, levers=None, output=None, **kw):
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
            **kw,
        ),
        audit,
        bundle,
    )


def test_no_ship_stops_after_patch_and_keeps_mitigation(tmp_path):
    orchestrator, audit, bundle = _orchestrator(tmp_path, ship=False)
    result = orchestrator.run("noship", bundle.experiment_start)
    events = audit.query("noship")
    kinds = [e.kind for e in events]
    assert result.patch is not None and result.canary is None and result.verification is None
    assert EventKind.patch_opened in kinds and EventKind.canary_update not in kinds
    report = next(e for e in events if e.kind == EventKind.report)
    assert report.payload["canary_status"] == "skipped"
    assert report.payload["clone_verification"] == "skipped"
    assert report.payload["mitigation_held"] == "retry_cap"
    # the mitigation is left holding production (only the experiment's release is recorded)
    undos = [e for e in events if e.kind == EventKind.action_undo]
    assert all(e.stage == Stage.experiment for e in undos)
    assert any(e.kind == EventKind.page_human and "awaits review" in e.summary for e in events)


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


class LaggingTelemetry:
    """Every read returns windows that end `lag_s` before the time asked for (ingest lag)."""

    def __init__(self, source, lag_s):
        self.source, self.lag_s = source, lag_s

    def window(self, start, end):
        return self.source.window(start, end)

    def series(self, start, end, step_s=5):
        from datetime import timedelta
        shift = timedelta(seconds=self.lag_s)
        return [fp.model_copy(update={"window_start": fp.window_start - shift, "window_end": fp.window_end - shift})
                for fp in self.source.series(start - shift, end - shift, step_s)]


def test_stale_telemetry_withholds_the_verdict_and_pages(tmp_path):
    bundle = load_fixture("storm")
    orchestrator, audit, _ = _orchestrator(tmp_path, telemetry=LaggingTelemetry(bundle.telemetry, lag_s=30))
    stale = []
    orchestrator._brain.judge = lambda triage, experiment, baseline, during, after_release: stale.append((during, after_release)) or bundle.verdict.model_copy(update={"incident_id": "stale"})

    orchestrator.run("stale", bundle.experiment_start)

    assert stale and stale[0] == ([], [])  # the judge never sees mis-aligned phases
    events = audit.query("stale")
    refused = next(e for e in events if e.kind == EventKind.refused and e.payload.get("stale_telemetry"))
    assert refused.stage == Stage.experiment and refused.actor == Actor.adapter
    assert refused.payload["lag_s"] >= 30 and "stale" in refused.summary


def test_fresh_telemetry_is_not_flagged(tmp_path):
    orchestrator, audit, bundle = _orchestrator(tmp_path)
    orchestrator.run("fresh", bundle.experiment_start)
    assert not any(e.payload.get("stale_telemetry") for e in audit.query("fresh"))


class ExplodingPatches(FixtureDevinAdapter):
    def propose(self, incident_id, verdict, triage):
        raise ConnectionError("Devin API: 502 Bad Gateway")


def test_unhandled_failure_is_audited_and_paged_before_propagating(tmp_path):
    output = []
    orchestrator, audit, bundle = _orchestrator(tmp_path, output=output)
    orchestrator._patches = ExplodingPatches()

    with pytest.raises(ConnectionError):
        orchestrator.run("crash", bundle.experiment_start)

    events = audit.query("crash")
    assert [e.kind for e in events[-2:]] == [EventKind.refused, EventKind.page_human]
    assert events[-2].payload["aborted"] is True
    assert events[-2].payload["error"].startswith("ConnectionError")
    assert events[-2].payload["actions_applied"] == 2  # experiment + kept mitigation, both TTL-bounded
    assert events[-1].stage == Stage.report
    assert output[-1] == "[report] aborted (ConnectionError) — paged human"


def test_budget_exceeded_is_not_paged_twice(tmp_path):
    bundle = load_fixture("storm")
    orchestrator, audit, _ = _orchestrator(tmp_path, budget=0)
    with pytest.raises(BudgetExceeded):
        orchestrator.run("budget-once", bundle.experiment_start)
    assert sum(e.kind == EventKind.page_human for e in audit.query("budget-once")) == 1


def test_missing_breach_is_a_precondition_not_an_incident(tmp_path):
    bundle = load_fixture("storm")
    healthy = next(fp for fp in bundle.telemetry.series(bundle.experiment_start, bundle.telemetry.last_window_end)
                   if not any(slo.breached for slo in fp.slos))

    class Healthy:
        def window(self, start, end):
            return healthy

        def series(self, start, end, step_s=5):
            return [healthy]

    orchestrator, audit, _ = _orchestrator(tmp_path, telemetry=Healthy())
    with pytest.raises(ValueError):
        orchestrator.run("no-breach", bundle.experiment_start)
    assert audit.query("no-breach") == []


def test_resume_continues_the_budget_and_releases_leftover_levers(tmp_path):
    bundle = load_fixture("storm")
    audit = JsonlSink(tmp_path / "audit.jsonl")
    clock = FixtureClock(bundle.experiment_start, bundle.telemetry.last_window_end)
    levers = FixtureLeverAdapter(clock=clock)
    # a previous run applied two levers, released one, and died before releasing the other
    released = levers.apply("retry_cap", {"max_retries": 0}, 50)
    levers.undo(released)
    leftover = levers.apply("db_failover", {}, 900)
    for handle, undone in ((released, True), (leftover, False)):
        audit.write(AuditEvent(incident_id="resume", stage=Stage.experiment, kind=EventKind.action_apply, actor=Actor.adapter,
                               summary=f"applied {handle.lever_id}", payload=handle.model_dump(mode="json"), action_id=handle.action_id))
        if undone:
            audit.write(AuditEvent(incident_id="resume", stage=Stage.experiment, kind=EventKind.action_undo, actor=Actor.adapter,
                                   summary="released", payload={**handle.model_dump(mode="json"), "status": "undone"}, action_id=handle.action_id))
    orchestrator = Orchestrator(levers, audit, FixtureDevinAdapter(), FixtureCanaryDeployer(), TerminalRenderer([].append),
                                bundle.telemetry, FixtureBrain(bundle.triage, bundle.experiment, bundle.verdict), clock, clock.sleep, 5)

    assert orchestrator.resume("resume") == 2
    assert orchestrator._actions == 2
    assert levers.status(leftover) == ActionStatus.undone
    events = audit.query("resume")
    undo = [e for e in events if e.kind == EventKind.action_undo and e.action_id == leftover.action_id]
    assert undo and "left over from a previous run" in undo[-1].summary
    note = next(e for e in events if e.payload.get("resumed"))
    assert note.payload == {"resumed": True, "actions_applied": 2, "leftover": ["db_failover"], "still_active": []}
    # the continued run then has only 3 actions left before it must page
    orchestrator._action_budget = 4
    with pytest.raises(BudgetExceeded):
        orchestrator.run("resume", bundle.experiment_start)


def test_resume_ignores_expired_and_already_released_levers(tmp_path):
    bundle = load_fixture("storm")
    audit = JsonlSink(tmp_path / "audit.jsonl")
    clock = FixtureClock(bundle.experiment_start, bundle.telemetry.last_window_end)
    levers = FixtureLeverAdapter(clock=clock)
    old = levers.apply("retry_cap", {"max_retries": 0}, 20)
    audit.write(AuditEvent(incident_id="expired", stage=Stage.experiment, kind=EventKind.action_apply, actor=Actor.adapter,
                           summary="applied", payload=old.model_dump(mode="json"), action_id=old.action_id))
    clock.sleep(60)  # the TTL has long passed: nothing to release
    orchestrator = Orchestrator(levers, audit, FixtureDevinAdapter(), FixtureCanaryDeployer(), TerminalRenderer([].append),
                                bundle.telemetry, FixtureBrain(bundle.triage, bundle.experiment, bundle.verdict), clock, clock.sleep, 5)
    assert orchestrator.resume("expired") == 1
    assert not any(e.kind == EventKind.action_undo for e in audit.query("expired"))


class RecordingPatches(FixtureDevinAdapter):
    def __init__(self):
        self.evidence = []

    def revise(self, incident_id, patch, evidence):
        self.evidence.append(evidence)
        return super().revise(incident_id, patch, evidence)


def test_canary_regression_sends_measured_numbers_and_z_scores_to_the_author(tmp_path):
    import json

    bundle = load_fixture("storm")
    orchestrator, audit, _ = _orchestrator(tmp_path, telemetry=BreachedTelemetry(bundle.telemetry))
    patches = RecordingPatches()
    orchestrator._patches = patches

    result = orchestrator.run("evidence", bundle.experiment_start)

    assert result.canary.status.value == "regressed"
    assert result.canary.evidence["breached_windows"] == 1 and result.canary.evidence["checkout_slo_threshold_ms"] == 1000
    assert result.canary.evidence["gateway_p99_ms_max"] > 1000
    text = patches.evidence[0]
    assert text.startswith("production canary regressed: checkout SLO breached")
    measured = json.loads(text.splitlines()[1].split("canary: ", 1)[1])
    assert measured == result.canary.evidence
    diagnosis_line = next(line for line in text.splitlines() if line.startswith("diagnosis H_meta"))
    rows = json.loads(diagnosis_line.split("observations: ", 1)[1])
    assert rows and {"experiment", "phase", "metric", "baseline", "measured", "sigma", "z", "direction"} <= set(rows[0])
    refused = next(e for e in audit.query("evidence") if e.stage == Stage.canary and e.kind == EventKind.refused)
    assert refused.payload["evidence"] == result.canary.evidence


class StubSimilar:
    def __init__(self, ranked=None, error=None):
        self.ranked, self.error, self.calls = ranked or [], error, []

    def find(self, fingerprint, *, exclude_incident_id, limit):
        self.calls.append((exclude_incident_id, limit))
        if self.error:
            raise self.error
        return self.ranked


def test_triage_records_similar_past_incidents_with_their_recorded_diagnosis(tmp_path):
    output = []
    orchestrator, audit, bundle = _orchestrator(tmp_path, output=output)
    orchestrator.run("past-1", bundle.experiment_start)  # a real earlier incident with an H_meta verdict
    orchestrator._actions = 0
    orchestrator._similar = StubSimilar([("past-1", 0.93), ("unknown-9", 0.4)])

    orchestrator.run("current", bundle.experiment_start)

    triage = next(e for e in audit.query("current") if e.stage == Stage.triage and e.kind == EventKind.triage)
    assert triage.payload["similar_incidents"] == [
        {"incident_id": "past-1", "score": 0.93, "diagnosis": "H_meta", "confirmed": True},
        {"incident_id": "unknown-9", "score": 0.4, "diagnosis": None, "confirmed": None},
    ]
    assert orchestrator._similar.calls == [("current", 3)]
    assert "[triage] looks like past-1 (0.93, was H_meta), unknown-9 (0.40)" in output


def test_similar_incident_search_failure_never_blocks_triage(tmp_path):
    output = []
    orchestrator, audit, bundle = _orchestrator(tmp_path, output=output)
    orchestrator._similar = StubSimilar(error=ConnectionError("es down"))
    result = orchestrator.run("lonely", bundle.experiment_start)
    assert result.diagnosis == "H_meta"
    triage = next(e for e in audit.query("lonely") if e.stage == Stage.triage and e.kind == EventKind.triage)
    assert triage.payload["similar_incidents"] == []
    assert "[triage] similar-incident search unavailable (ConnectionError)" in output
