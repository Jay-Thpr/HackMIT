import pytest
from faultline_contracts import EventKind, JsonlSink, LeverError, Stage

from faultline_product.adapters import (
    FixtureBrain,
    FixtureClock,
    FixtureDevinAdapter,
    FixtureLeverAdapter,
)
from faultline_product.fixtures import load_fixture
from faultline_product.orchestrator import BudgetExceeded, Orchestrator
from faultline_product.renderer import TerminalRenderer


def _orchestrator(tmp_path, *, experiment=None, telemetry=None, budget=5, levers=None, output=None):
    bundle = load_fixture("storm")
    telemetry = telemetry or bundle.telemetry
    clock = FixtureClock(
        bundle.telemetry.first_breach().window_end, bundle.telemetry.last_window_end
    )
    brain = FixtureBrain(bundle.triage, experiment or bundle.experiment, bundle.verdict)
    audit = JsonlSink(tmp_path / "audit.jsonl")
    levers = levers or FixtureLeverAdapter(clock=clock)
    output = output if output is not None else []
    return (
        Orchestrator(
            levers,
            audit,
            FixtureDevinAdapter(),
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
    result = orchestrator.run("refused", bundle.telemetry.first_breach().window_end)
    assert result.diagnosis == "refused"
    assert any(event.kind == EventKind.refused for event in audit.query("refused"))


def test_budget_exceeded_pages_human(tmp_path):
    bundle = load_fixture("storm")
    orchestrator, audit, _ = _orchestrator(tmp_path, budget=0)
    with pytest.raises(BudgetExceeded):
        orchestrator.run("budget", bundle.telemetry.first_breach().window_end)
    assert any(event.kind == EventKind.page_human for event in audit.query("budget"))


def test_kept_mitigation_has_mitigate_action_id(tmp_path):
    orchestrator, audit, bundle = _orchestrator(tmp_path)
    orchestrator.run("kept", bundle.telemetry.first_breach().window_end)
    events = audit.query("kept")
    assert any(
        event.stage == Stage.mitigate and event.kind == EventKind.action_apply and event.action_id
        for event in events
    )


class BreachedTelemetry:
    def __init__(self, source):
        self.source = source

    def window(self, start, end):
        return self.source.first_breach()

    def series(self, start, end, step_s=5):
        return self.source.series(start, end, step_s)


def test_canary_regression_auto_undoes_and_refuses(tmp_path):
    bundle = load_fixture("storm")
    orchestrator, audit, _ = _orchestrator(tmp_path, telemetry=BreachedTelemetry(bundle.telemetry))
    orchestrator.run("regression", bundle.telemetry.first_breach().window_end)
    events = audit.query("regression")
    assert any(
        event.kind == EventKind.action_undo and event.stage == Stage.canary for event in events
    )
    assert any(event.kind == EventKind.refused and event.stage == Stage.canary for event in events)


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

    result = orchestrator.run("canary-refused", bundle.telemetry.first_breach().window_end)

    assert result.patch is not None
    events = audit.query("canary-refused")
    canary_events = [event for event in events if event.stage == Stage.canary]
    assert [event.kind for event in canary_events] == [EventKind.refused, EventKind.page_human]
    assert not any(event.kind == EventKind.action_apply for event in canary_events)
    assert events[-1].kind == EventKind.report
    assert "[canary] canary_weight refused: orders-v2 is not running — paged human" in output
