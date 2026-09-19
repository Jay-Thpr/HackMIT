from faultline_contracts import (
    EventKind,
    JsonlSink,
    LeverAdapter,
    TelemetrySource,
    experiment_windows,
)
from faultline_product.adapters import (
    FixtureBrain,
    FixtureClock,
    FixtureDevinAdapter,
    FixtureLeverAdapter,
)
from faultline_product.cli import main
from faultline_product.fixtures import load_fixture
from faultline_product.orchestrator import Orchestrator
from faultline_product.renderer import TerminalRenderer
from faultline_product.report import render_report


def test_storm_flow_uses_contract_boundaries(tmp_path):
    bundle = load_fixture("storm")
    audit = JsonlSink(tmp_path / "audit.jsonl")
    clock = FixtureClock(
        bundle.telemetry.first_breach().window_end,
        bundle.telemetry.last_window_end,
    )
    output = []
    levers = FixtureLeverAdapter(clock=clock)
    assert isinstance(levers, LeverAdapter)
    assert isinstance(bundle.telemetry, TelemetrySource)
    brain = FixtureBrain(bundle.triage, bundle.experiment, bundle.verdict)
    orchestrator = Orchestrator(
        levers=levers,
        audit=audit,
        patches=FixtureDevinAdapter(),
        renderer=TerminalRenderer(output.append),
        telemetry=bundle.telemetry,
        brain=brain,
        clock=clock,
        sleep=clock.sleep,
    )

    result = orchestrator.run("demo-storm-001", bundle.telemetry.first_breach().window_end)

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
    experiment_events = [
        event
        for event in events
        if event.experiment_id == bundle.experiment.id and event.stage == 4
    ]
    assert {event.action_id for event in experiment_events if event.action_id} == {
        experiment_events[0].action_id
    }
    assert output[3] == "[experiment] retry_cap on: {'max_retries': 0}"
    assert output[4] == "[experiment] retry_cap released"
    assert output[5].startswith("[observe] after-release window collected")


def test_report_is_rebuilt_from_audit_log(tmp_path, capsys):
    audit_path = tmp_path / "audit.jsonl"
    exit_code = main(
        [
            "--audit-log",
            str(audit_path),
            "watch",
            "--fixture",
            "storm",
            "--incident",
            "demo-storm-001",
        ]
    )
    assert exit_code == 0

    report = render_report(JsonlSink(audit_path), "demo-storm-001")
    assert "Diagnosis: H_meta" in report
    assert "experiment_start" in report
    assert "experiment_end" in report
    assert "Patch: devin://task/demo-storm-001" in report
    assert "[report] ready" in capsys.readouterr().out


def test_duplicate_incident_is_rejected(tmp_path, capsys):
    audit = tmp_path / "audit.jsonl"
    assert main(["--audit-log", str(audit), "watch", "--incident", "duplicate"]) == 0
    assert main(["--audit-log", str(audit), "watch", "--incident", "duplicate"]) == 2
    assert "incident already exists; pick a new id" in capsys.readouterr().out


def test_investigate_and_experiment_commands(tmp_path, capsys):
    audit = tmp_path / "audit.jsonl"
    assert main(["--audit-log", str(audit), "investigate", "--incident", "investigate"]) == 0
    assert "Experiment: retry_cap_0_20s" in capsys.readouterr().out
    assert (
        main(
            [
                "--audit-log",
                str(audit),
                "experiment",
                "--id",
                "retry_cap_0_20s",
                "--incident",
                "experiment",
            ]
        )
        == 0
    )
    assert "Capping retries" in capsys.readouterr().out


def test_unreachable_sandbox_control_service(tmp_path, capsys):
    result = main(
        [
            "--audit-log",
            str(tmp_path / "audit.jsonl"),
            "watch",
            "--levers",
            "sandbox",
            "--control-url",
            "http://127.0.0.1:1",
            "--incident",
            "unreachable",
        ]
    )
    assert result == 2
    assert "sandbox control service not reachable" in capsys.readouterr().out
