import importlib.util
import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "comparison_replay.py"
SPEC = importlib.util.spec_from_file_location("comparison_replay", PATH)
replay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replay
SPEC.loader.exec_module(replay)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source"
    origin = replay.fake_source(path, "storm")
    return path, origin


def test_virtual_clock_does_not_sleep_and_rejects_invalid_advances():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = replay.ReplayClock(start)
    clock.sleep(600)
    assert clock.utcnow() == start + timedelta(seconds=600)
    for invalid in (-1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            clock.sleep(invalid)


@pytest.mark.parametrize("arm", replay.ARMS)
def test_real_observer_and_worker_paths_run_without_network_or_wall_sleep(source, tmp_path, monkeypatch, arm):
    path, origin = source
    original = (path / "telemetry.json").read_bytes()
    real_observer = replay.runtime.run_observer
    called = []

    def traced(*args, **kwargs):
        called.append(args[0])
        return real_observer(*args, **kwargs)

    monkeypatch.setattr(replay.runtime, "run_observer", traced)
    monkeypatch.setattr(time, "sleep", lambda *_: pytest.fail("real sleep attempted"))
    destination = tmp_path / arm
    report = replay.replay_one(path, destination, arm, "plain", origin=origin)
    assert report["passed"] is True
    assert report["worker_result"]["status"] == "completed"
    assert called == [arm]
    assert report["provider_calls"] == 2
    assert report["virtual_end_s"] == 120
    assert (path / "telemetry.json").read_bytes() == original
    assert report["source"] == "offline-fake-provider"
    assert "accuracy" not in report and "correct" not in report
    events = [json.loads(line) for line in (destination / "events.jsonl").read_text().splitlines()]
    assert sum(event["kind"] == "conclusion" for event in events) == 2
    assert all(event.get("environment") == "observer" for event in events)


@pytest.mark.parametrize("arm", replay.ARMS)
def test_recorded_future_windows_are_revealed_only_as_virtual_time_advances(source, tmp_path, arm):
    path, origin = source
    report = replay.replay_one(path, tmp_path / arm, arm, "plain", origin=origin)
    reads = report["telemetry_reads"]
    assert len(reads) == 2 and reads[1]["window_count"] > reads[0]["window_count"]
    assert all(datetime.fromisoformat(row["latest_window_end"]) <= datetime.fromisoformat(row["at"]) for row in reads)
    calls = json.loads((tmp_path / arm / "provider-calls.json").read_text())
    assert calls[0]["latest_window_end"] != calls[1]["latest_window_end"]
    if arm == "elastic":
        assert calls[1]["conversation_id"] == "offline-conversation"
        assert calls[0]["request"]["configuration_overrides"]["enable_elastic_capabilities"] is False
    else:
        assert calls[0]["structured_output_requested"] is True


def test_network_and_subprocess_guards_fail_closed():
    with replay.offline_boundary():
        with pytest.raises(replay.OfflineBoundaryError):
            socket.create_connection(("localhost", 9900))
        with socket.socket() as sock:
            with pytest.raises(replay.OfflineBoundaryError):
                sock.connect(("127.0.0.1", 9900))
        with pytest.raises(replay.OfflineBoundaryError):
            socket.getaddrinfo("example.invalid", 443)
        with pytest.raises(replay.OfflineBoundaryError):
            subprocess.Popen(["docker", "ps"])
        with pytest.raises(replay.OfflineBoundaryError):
            replay.runtime.run_probe(None)


def test_patches_restore_process_state_and_do_not_read_real_keys(source, tmp_path, monkeypatch):
    path, origin = source
    monkeypatch.setenv("OPENAI_API_KEY", "outside-key-not-for-replay")
    original_factory = replay.openai.OpenAI
    original_clock = replay.runtime.utcnow
    original_connect = socket.socket.connect
    replay.replay_one(path, tmp_path / "plain", "observe", "plain", origin=origin)
    assert replay.os.environ["OPENAI_API_KEY"] == "outside-key-not-for-replay"
    assert replay.openai.OpenAI is original_factory
    assert replay.runtime.utcnow is original_clock
    assert socket.socket.connect is original_connect
    artifacts = (tmp_path / "plain" / "provider-calls.json").read_text()
    assert "outside-key-not-for-replay" not in artifacts


@pytest.mark.parametrize("scenario", ["fenced", "preamble", "repair", "invalid_metric", "malformed"])
def test_scripted_raw_replies_reach_real_validation_without_replay_repair(source, tmp_path, monkeypatch, scenario):
    path, origin = source
    validator = replay.agents.validate_diagnosis
    replies = []

    def traced(text, windows):
        replies.append(text)
        return validator(text, windows)

    monkeypatch.setattr(replay.agents, "validate_diagnosis", traced)
    report = replay.replay_one(path, tmp_path / scenario, "observe", scenario, origin=origin)
    assert replies
    if scenario == "fenced":
        assert replies[0].startswith("```json")
    elif scenario == "preamble":
        assert replies[0].startswith("Here is the assessment:")
    elif scenario in ("repair", "invalid_metric"):
        assert replay.MISSING_METRIC in replies[0]
    else:
        assert replies[0] == "{not valid JSON"
    assert report["passed"] == all(report["checks"].values())
    if scenario in ("repair", "invalid_metric"):
        assert "repair_budget_is_two_calls" in report["checks"] or "one_immediate_corrective_reask" in report["checks"]


@pytest.mark.parametrize("arm", replay.ARMS)
def test_error_reporting_is_checked_against_real_worker_artifacts(source, tmp_path, arm):
    path, origin = source
    destination = tmp_path / arm
    report = replay.replay_one(path, destination, arm, "provider_error", origin=origin)
    result = json.loads((destination / "worker-result.json").read_text())
    assert result["status"] == "error" and report["expected_status"] == "error"
    assert report["checks"]["worker_error_has_detail"] == bool(result.get("detail"))
    assert report["checks"]["traceback_artifact_exists"] == (destination / "traceback.txt").is_file()
    assert report["checks"]["provider_was_called"] is True


@pytest.mark.parametrize("arm", replay.ARMS)
def test_late_reply_hits_real_deadline_check(source, tmp_path, arm):
    path, origin = source
    report = replay.replay_one(path, tmp_path / arm, arm, "late", origin=origin)
    assert report["worker_result"]["status"] == "timeout"
    assert report["virtual_end_s"] == 121
    assert report["provider_calls"] == 1


def test_source_and_existing_outputs_cannot_be_overwritten(source, tmp_path):
    path, origin = source
    with pytest.raises(ValueError, match="source run"):
        replay.replay_one(path, path / "replay", "observe", "plain", origin=origin)
    output = tmp_path / "output"
    replay.replay_one(path, output, "observe", "plain", origin=origin)
    with pytest.raises(FileExistsError):
        replay.replay_one(path, output, "observe", "plain", origin=origin)
    with pytest.raises(ValueError):
        replay.replay_one(path, tmp_path / "probe", "probe", "plain", origin=origin)


def test_cli_is_offline_only_and_plain_fake_suite_passes(tmp_path, capsys):
    assert replay.main(["--fake-world", "degraded", "--scenarios", "plain", "--output", str(tmp_path)]) == 0
    assert "observe" in capsys.readouterr().out
    suites = list(tmp_path.glob("replay-*/replay-suite.json"))
    assert len(suites) == 1
    data = json.loads(suites[0].read_text())
    assert data["source"] == "offline-fake-provider" and len(data["reports"]) == 2
    with pytest.raises(SystemExit):
        replay.main(["--execute"])
