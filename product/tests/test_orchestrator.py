import pytest
from faultline_contracts import EventKind, JsonlSink, Stage
from faultline_product.adapters import (
    FixtureBrain,
    FixtureClock,
    FixtureDevinAdapter,
    FixtureLeverAdapter,
)
from faultline_product.fixtures import load_fixture
from faultline_product.orchestrator import BudgetExceeded, Orchestrator
from faultline_product.renderer import TerminalRenderer


def _orchestrator(tmp_path, *, experiment=None, telemetry=None, budget=5):
    bundle = load_fixture("storm")
    telemetry = telemetry or bundle.telemetry
    clock = FixtureClock(
        bundle.telemetry.first_breach().window_end, bundle.telemetry.last_window_end
    )
    brain = FixtureBrain(bundle.triage, experiment or bundle.experiment, bundle.verdict)
    audit = JsonlSink(tmp_path / "audit.jsonl")
    return (
        Orchestrator(
            FixtureLeverAdapter(clock=clock),
            audit,
            FixtureDevinAdapter(),
            TerminalRenderer(lambda _: None),
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
