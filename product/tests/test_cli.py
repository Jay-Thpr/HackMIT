from datetime import datetime, timedelta, timezone

from faultline_contracts import EventKind, JsonlSink, LeverAdapter, TelemetrySource, experiment_windows

from faultline_product.adapters import FixtureDevinAdapter, FixtureLeverAdapter
from faultline_product.cli import main
from faultline_product.fixtures import load_fixture
from faultline_product.orchestrator import Orchestrator
from faultline_product.renderer import TerminalRenderer
from faultline_product.report import render_report


class StepClock:
    def __init__(self):
        self.now = datetime(2026, 9, 19, 15, 0, tzinfo=timezone.utc)

    def __call__(self):
        current = self.now
        self.now += timedelta(milliseconds=1)
        return current


def test_storm_flow_uses_contract_boundaries(tmp_path):
    bundle = load_fixture("storm")
    audit = JsonlSink(tmp_path / "audit.jsonl")
    clock = StepClock()
    output = []
    levers = FixtureLeverAdapter(clock=clock)
    assert isinstance(levers, LeverAdapter)
    assert isinstance(bundle.telemetry, TelemetrySource)
    orchestrator = Orchestrator(
        levers=levers,
        audit=audit,
        patches=FixtureDevinAdapter(),
        renderer=TerminalRenderer(output.append),
        clock=clock,
    )

    result = orchestrator.run(
        incident_id="demo-storm-001",
        fingerprint=bundle.telemetry.first_breach(),
        triage=bundle.triage,
        experiment=bundle.experiment,
        verdict=bundle.verdict,
    )

    events = audit.query(result.incident_id)
    kinds = [event.kind for event in events]
    assert kinds[:6] == [
        EventKind.detect,
        EventKind.triage,
        EventKind.experiment_start,
        EventKind.action_apply,
        EventKind.action_undo,
        EventKind.experiment_end,
    ]
    assert kinds[-1] == EventKind.report
    assert experiment_windows(events)[0].release is not None
    experiment_events = [event for event in events if event.experiment_id == bundle.experiment.id]
    assert {event.action_id for event in experiment_events if event.action_id} == {
        experiment_events[0].action_id
    }
    assert output[3] == "[experiment] cap on: retry_cap max_retries=0"
    assert output[4] == "[experiment] cap released"
    assert output[5].startswith("[observe] after-release window collected")


def test_report_is_rebuilt_from_audit_log(tmp_path, capsys):
    audit_path = tmp_path / "audit.jsonl"
    exit_code = main(
        ["--audit-log", str(audit_path), "watch", "--fixture", "storm", "--incident", "demo-storm-001"]
    )
    assert exit_code == 0

    report = render_report(JsonlSink(audit_path), "demo-storm-001")
    assert "Diagnosis: H_meta" in report
    assert "experiment_start" in report
    assert "experiment_end" in report
    assert "Patch: devin://task/demo-storm-001" in report
    assert "[report] ready" in capsys.readouterr().out
