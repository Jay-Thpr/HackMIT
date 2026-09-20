from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest


@pytest.fixture(autouse=True)
def isolate_elasticsearch(monkeypatch):
    monkeypatch.setattr("faultline_product.cli.load_repo_dotenv", lambda _: None)
    for name in ("FAULTLINE_ELASTICSEARCH_URL", "FAULTLINE_ELASTICSEARCH_API_KEY",
                 "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL", "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_API_KEY",
                 "FAULTLINE_ELASTICSEARCH_MIRROR_URL", "FAULTLINE_ELASTICSEARCH_MIRROR_API_KEY",
                 "FAULTLINE_MIRROR_OUTBOX_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("command", ["investigate", "watch", "experiment"])
def test_fixture_never_persists_as_production(tmp_path, monkeypatch, command):
    monkeypatch.setenv("FAULTLINE_ELASTICSEARCH_URL", "https://a.test")
    monkeypatch.setenv("FAULTLINE_ELASTICSEARCH_API_KEY", "a-key")
    monkeypatch.setenv("FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL", "https://b.test")
    monkeypatch.setenv("FAULTLINE_OBSERVABILITY_ELASTICSEARCH_API_KEY", "b-key")
    factory = Mock()
    monkeypatch.setattr("faultline_product.cli._persistence_client", factory)
    extra = ["--id", "retry_cap_0_20s"] if command == "experiment" else []
    assert main(["--audit-log", str(tmp_path / "audit.jsonl"), command, "--incident", "smoke-fixture", *extra]) == 0
    factory.assert_not_called()


def test_ui_never_initializes_writing_client(tmp_path, monkeypatch):
    import uvicorn
    from faultline_product import api, cli

    monkeypatch.setenv("FAULTLINE_ELASTICSEARCH_URL", "https://a.test")
    persistence = Mock()
    monkeypatch.setattr(cli, "_persistence_client", persistence)
    monkeypatch.setattr(api, "build_store", Mock(return_value=None))
    monkeypatch.setattr(uvicorn, "run", Mock())
    assert main(["--audit-log", str(tmp_path / "audit.jsonl"), "ui"]) == 0
    persistence.assert_not_called()
    uvicorn.run.assert_called_once()


def test_cli_overrides_environment_and_template_failure_is_nonfatal(monkeypatch, caplog):
    from faultline_product import cli

    primary = Mock(spec=["close", "put_index_template"])
    factory = Mock(return_value=primary)
    monkeypatch.setattr(cli, "client_from_env", factory)
    monkeypatch.setattr(cli, "ensure_index_templates", Mock(side_effect=RuntimeError("secret")))
    monkeypatch.setenv("FAULTLINE_ELASTICSEARCH_URL", "https://env.test")
    monkeypatch.setenv("FAULTLINE_ELASTICSEARCH_API_KEY", "env-key")
    assert cli._persistence_client("https://flag.test", "flag-key") is primary
    env = factory.call_args.args[0]
    assert env["FAULTLINE_ELASTICSEARCH_URL"] == "https://flag.test"
    assert env["FAULTLINE_ELASTICSEARCH_API_KEY"] == "flag-key"
    cli.ensure_index_templates.assert_called_once_with(primary)
    assert "RuntimeError" in caplog.text and "secret" not in caplog.text


def test_mirror_outbox_creation_failure_degrades_to_primary(monkeypatch, caplog):
    from faultline_product import cli
    from faultline_telemetry import factory as elastic_factory
    primary, secondary = Mock(), Mock()
    monkeypatch.setenv("FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL", "https://b.test")
    monkeypatch.setenv("FAULTLINE_OBSERVABILITY_ELASTICSEARCH_API_KEY", "secret")
    monkeypatch.setattr(elastic_factory, "HttpElasticsearchClient", Mock(side_effect=[primary, secondary]))
    monkeypatch.setattr(cli, "ensure_index_templates", Mock())
    monkeypatch.setattr(elastic_factory, "MirroredElasticsearchClient", Mock(side_effect=OSError("secret path")))
    assert cli._persistence_client("https://a.test", "key") is primary
    secondary.close.assert_called_once()
    primary.close.assert_not_called()
    assert "primary only (OSError)" in caplog.text
    assert "secret" not in caplog.text


def test_partial_mirror_configuration_is_visible_and_not_activated(monkeypatch, caplog):
    from faultline_product import cli
    from faultline_telemetry import factory as elastic_factory
    primary = Mock()
    factory = Mock(return_value=primary)
    monkeypatch.setattr(elastic_factory, "HttpElasticsearchClient", factory)
    monkeypatch.setattr(cli, "ensure_index_templates", Mock())
    monkeypatch.setenv("FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL", "https://b.test")
    assert cli._persistence_client("https://a.test", "key") is primary
    assert factory.call_count == 1
    assert "incomplete" in caplog.text


def test_mirror_shared_client_and_shutdown(monkeypatch, tmp_path):
    from faultline_product import cli
    from faultline_telemetry import factory as elastic_factory
    primary, secondary = Mock(), Mock()
    mirrored = Mock(primary=primary)
    monkeypatch.setenv("FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL", "https://b.test")
    monkeypatch.setenv("FAULTLINE_OBSERVABILITY_ELASTICSEARCH_API_KEY", "b-key")
    monkeypatch.setenv("FAULTLINE_MIRROR_OUTBOX_DIR", str(tmp_path))
    monkeypatch.setattr(elastic_factory, "HttpElasticsearchClient", Mock(side_effect=[primary, secondary]))
    setup = Mock()
    monkeypatch.setattr(cli, "ensure_index_templates", setup)
    factory = Mock(return_value=mirrored)
    monkeypatch.setattr(elastic_factory, "MirroredElasticsearchClient", factory)
    assert cli._persistence_client("https://a.test", "key") is mirrored
    assert factory.call_args.args[:2] == (primary, secondary)
    setup.assert_called_once_with(primary)
    monkeypatch.setattr(cli, "_persistence_client", lambda *a: mirrored)
    monkeypatch.setenv("FAULTLINE_ELASTICSEARCH_URL", "https://a.test")
    telemetry = Mock()
    telemetry.healthz.return_value = False
    monkeypatch.setattr(cli, "LiveTelemetrySource", Mock(return_value=telemetry))
    assert main(["--audit-log", str(tmp_path / "audit.jsonl"), "watch", "--telemetry", "sandbox", "--levers", "sandbox"]) == 2
    mirrored.close.assert_called_once()
    assert cli.LiveTelemetrySource.call_args.kwargs["writer"]._client is mirrored

import pytest
from faultline_contracts import (
    EventKind,
    JsonlSink,
    LeverAdapter,
    TelemetrySource,
    experiment_windows,
)
from faultline_product.adapters import (
    FixtureBrain,
    FixtureCanaryDeployer,
    FixtureClock,
    FixtureDevinAdapter,
    FixtureLeverAdapter,
)
from faultline_product.cli import build_parser, main
from faultline_product.fixtures import load_fixture
from faultline_product.orchestrator import Orchestrator
from faultline_product.renderer import TerminalRenderer
from faultline_product.report import render_report


def test_fixture_clock_real_sleep_advances_fixture_time(monkeypatch):
    start = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)
    clock = FixtureClock(start)
    monkeypatch.setattr("faultline_product.adapters.fixture.time.sleep", lambda seconds: None)

    clock.real_sleep(0)
    assert clock.now == start
    clock.real_sleep(0.01)
    assert clock.now == start + timedelta(seconds=0.01)


def test_storm_flow_uses_contract_boundaries(tmp_path):
    bundle = load_fixture("storm")
    audit = JsonlSink(tmp_path / "audit.jsonl")
    clock = FixtureClock(
        bundle.experiment_start,
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
        canary_deployer=FixtureCanaryDeployer(),
        renderer=TerminalRenderer(output.append),
        telemetry=bundle.telemetry,
        brain=brain,
        clock=clock,
        sleep=clock.sleep,
    )

    result = orchestrator.run("demo-storm-001", bundle.experiment_start)

    events = audit.query(result.incident_id)
    kinds = [event.kind for event in events]
    assert kinds[:7] == [
        EventKind.detect,
        EventKind.triage,
        EventKind.triage,  # planner candidate table (actor math)
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
    start = next(event for event in experiment_events if event.kind == EventKind.experiment_start)
    assert {event.action_id for event in experiment_events if event.action_id} == {start.action_id}
    assert output[2] == "[triage] source: fixture"
    assert output[4] == "[experiment] retry_cap on: {'max_retries': 0}"
    assert output[5] == "[experiment] retry_cap released"
    assert output[6].startswith("[observe] after-release window collected")


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
    output = capsys.readouterr().out
    assert "[judge] H_meta confirmed" in output
    assert "[report] ready" in output


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


def test_unreachable_sandbox_profile_reports_health_error(tmp_path, capsys):
    result = main(
        [
            "--audit-log",
            str(tmp_path / "audit.jsonl"),
            "watch",
            "--levers",
            "sandbox",
            "--telemetry",
            "sandbox",
            "--brain",
            "live",
            "--control-url",
            "http://127.0.0.1:1",
            "--sandbox-host",
            "sandbox.invalid",
            "--incident",
            "unreachable",
        ]
    )
    assert result == 2
    assert "sandbox /stats not reachable" in capsys.readouterr().out


def test_sandbox_cli_accepts_elasticsearch_and_clone_metadata_options():
    args = build_parser().parse_args(
        [
            "watch", "--telemetry", "sandbox", "--levers", "sandbox",
            "--elasticsearch-url", "http://elastic:9200", "--clone-id", "clone-h-meta",
        ]
    )
    assert args.elasticsearch_url == "http://elastic:9200"
    assert args.clone_id == "clone-h-meta"


def test_watch_resume_requires_an_existing_incident_and_continues_it(tmp_path, capsys):
    audit = tmp_path / "audit.jsonl"
    assert main(["--audit-log", str(audit), "watch", "--incident", "again", "--resume"]) == 2
    assert "nothing to resume" in capsys.readouterr().out
    assert main(["--audit-log", str(audit), "watch", "--incident", "again"]) == 0
    assert main(["--audit-log", str(audit), "watch", "--incident", "again"]) == 2
    assert "watch --resume" in capsys.readouterr().out
    # the resumed run keeps the first run's 3 actions on the budget, so its own experiment,
    # mitigation and canary would be 6 > 5: it stops and pages instead of acting again
    assert main(["--audit-log", str(audit), "watch", "--incident", "again", "--resume"]) == 2
    assert "action budget exceeded" in capsys.readouterr().out
    from faultline_contracts import JsonlSink

    events = JsonlSink(audit).query("again")
    note = next(e for e in events if e.payload.get("resumed"))
    assert note.payload["actions_applied"] == 3
    assert sum(e.kind == EventKind.action_apply for e in events) == 5
    assert any(e.kind == EventKind.page_human and "budget" in e.summary for e in events)


def test_pager_command_flag_pages_every_page_human(tmp_path, monkeypatch):
    from faultline_product.adapters import pager

    runs = []
    monkeypatch.setattr(pager.subprocess, "run", lambda argv, **kw: runs.append((argv, kw["input"])))
    audit = tmp_path / "audit.jsonl"
    # a budget of zero pages on the first action
    from faultline_product import cli

    monkeypatch.setattr(cli, "Orchestrator", lambda **kw: cli_orchestrator_with_budget(kw, 0))
    assert main(["--audit-log", str(audit), "--pager-command", "notify --title Faultline", "watch", "--incident", "paged"]) == 2
    assert runs and runs[0][0] == ["notify", "--title", "Faultline"]
    assert '"incident_id": "paged"' in runs[0][1]


def cli_orchestrator_with_budget(kwargs, budget):
    from faultline_product.orchestrator import Orchestrator

    return Orchestrator(**kwargs, action_budget=budget)


def test_max_clones_choices_match_lab_capacity():
    parser = build_parser()
    for bad in ("0", "-1", "4"):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["watch", "--max-clones", bad])
        assert exc.value.code == 2
    for good in ("1", "2", "3"):
        assert parser.parse_args(["watch", "--max-clones", good]).max_clones == int(good)
    assert parser.parse_args(["watch"]).max_clones == 1
