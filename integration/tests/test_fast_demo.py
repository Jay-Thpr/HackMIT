from datetime import datetime, timedelta, timezone

import pytest
from faultline_contracts import Actor, AuditEvent, EventKind, Stage

from fast_demo import qualify


T0 = datetime(2026, 9, 20, tzinfo=timezone.utc)


def event(kind, stage, seconds, payload, action_id=None, incident_id="fast-test"):
    return AuditEvent(incident_id=incident_id, kind=kind, stage=stage, ts=T0 + timedelta(seconds=seconds),
                      actor=Actor.orchestrator, summary="test evidence", payload=payload, action_id=action_id)


def successful_events():
    return [
        event(EventKind.verdict, Stage.experiment, 105, {"diagnosis": "H_meta", "confirmed": True}),
        event(EventKind.patch_opened, Stage.patch, 110,
              {"provider": "prepared", "reference": "prepared:sha256:test"}),
        event(EventKind.action_apply, Stage.canary, 150,
              {"lever_id": "canary_weight", "params": {"v2_weight": 0.05}}, "canary-1"),
        event(EventKind.action_undo, Stage.canary, 270, {"status": "undone"}, "canary-1"),
        event(EventKind.canary_update, Stage.canary, 270.5,
              {"evidence": {"canary_split_status": "compatible", "canary_expected_share": 0.05,
                            "canary_observed_share_estimate": 0.051}}, "canary-1"),
        event(EventKind.report, Stage.report, 271,
              {"diagnosis": "H_meta", "clone_verification": "passed", "canary_status": "passed"}),
    ]


def result(events=None, elapsed=272, returncode=0, timed_out=False):
    return qualify(successful_events() if events is None else events, incident_id="fast-test",
                   elapsed_s=elapsed, returncode=returncode, timed_out=timed_out)


def test_complete_measured_run_below_budget_passes():
    assert result()["status"] == "passed"
    assert result()["canary_observed_and_released"] is True


@pytest.mark.parametrize("elapsed", [300, 301, float("inf"), float("nan"), -1])
def test_budget_is_strict_and_invalid_durations_never_pass(elapsed):
    assert result(elapsed=elapsed)["status"] != "passed"


@pytest.mark.parametrize("missing", range(6))
def test_every_gate_requires_recorded_evidence(missing):
    events = successful_events()
    events.pop(missing)
    assert result(events)["status"] == "failed"


@pytest.mark.parametrize("field,value", [("clone_verification", "skipped"), ("canary_status", "refused"),
                                        ("diagnosis", "H_db")])
def test_completed_process_is_not_success_if_safety_gate_did_not_pass(field, value):
    events = successful_events()
    events[-1].payload[field] = value
    assert result(events)["status"] == "failed"


def test_unconfirmed_verdict_cannot_pass():
    events = successful_events()
    events[0].payload["confirmed"] = False
    assert result(events)["status"] == "failed"


def test_short_canary_is_not_qualified():
    events = successful_events()
    events[3].ts = events[2].ts + timedelta(seconds=119)
    assert result(events)["status"] == "failed"


def test_unrelated_release_is_not_confirmation():
    events = successful_events()
    events[3].action_id = "different-action"
    assert result(events)["status"] == "failed"


def test_unrelated_incident_cannot_supply_missing_evidence():
    events = successful_events()
    events[-1].incident_id = "another-incident"
    assert result(events)["status"] == "failed"


def test_failure_or_timeout_wins_over_positive_audit():
    assert result(returncode=1)["status"] == "failed"
    assert result(timed_out=True)["status"] == "timeout"


def test_wrong_or_unrelated_canary_split_cannot_qualify():
    events = successful_events()
    events[4].payload["evidence"]["canary_split_status"] = "mismatched"
    assert result(events)["status"] == "failed"
    events = successful_events()
    events[4].action_id = "different-canary"
    assert result(events)["status"] == "failed"


def test_recorded_share_is_reported_without_recomputation():
    assert result()["canary_observed_share_estimate"] == 0.051


def test_later_failed_report_wins_over_earlier_success():
    events = successful_events()
    events.append(event(EventKind.report, Stage.report, 273, {"diagnosis": "H_meta", "canary_status": "refused"}))
    assert result(events)["status"] == "failed"
