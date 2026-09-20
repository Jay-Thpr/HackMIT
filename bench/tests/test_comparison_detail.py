"""Event detail shown in the replay panel, and the backfill that restores it.

Recordings made before RecordingAudit carried detail render the Faultline arms as bare
titles next to an observer arm quoting its whole answer. `audit_detail` is the shared
formatter; `comparison_enrich` replays it over a finished recording using each run's own
audit log.
"""

import json
import sys
from pathlib import Path

from faultline_bench.comparison import Event, Protocol, Recording, Run, audit_detail, new_recording

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from comparison_enrich import NOTE, enrich  # noqa: E402

TRIAGE = {
    "triage": {
        "reasoning": "Both stories fit the telemetry.",
        "hypotheses": [{"id": "H_meta", "label": "retry storm", "description": "retries sustain it"}],
        "predictions": [{
            "hypothesis_id": "H_meta", "experiment_id": "retry_cap_0_20s",
            "during": [{"metric": "svc.gateway.p99_ms", "direction": "down"}],
            "after_release": [{"metric": "svc.gateway.p99_ms", "direction": "flat"}],
            "confirms_if": {"phase": "after_release", "metric": "svc.gateway.p99_ms", "expect": "within_baseline"},
        }],
    },
    "ambiguous": True,
}
VERDICT = {
    "diagnosis": "H_meta", "confirmed": True,
    "support": [{"hypothesis_id": "H_meta", "support": 0.5, "confirmed": True}],
    "observations": [{"phase": "during", "metric": "svc.gateway.p99_ms", "baseline": 2494.774,
                      "measured": 140.486, "sigma": 249.4, "z": -9.4374, "direction": "down"}],
}


def test_triage_detail_carries_reasoning_hypotheses_and_predictions():
    detail = audit_detail(TRIAGE)

    assert "Both stories fit the telemetry." in detail
    assert "H_meta — retry storm: retries sustain it" in detail
    assert "while held svc.gateway.p99_ms down" in detail
    assert "confirms if after_release svc.gateway.p99_ms is within_baseline" in detail
    assert "ambiguous: True" in detail


def test_verdict_detail_carries_the_measured_z_table():
    detail = audit_detail(VERDICT)

    assert "support H_meta: 0.5, confirmed=True" in detail
    assert "during svc.gateway.p99_ms: baseline 2494.774 → measured 140.486 (z -9.437, down)" in detail


def test_planner_detail_names_separation_and_the_selected_probe():
    detail = audit_detail({"planner": True, "candidates": [
        {"experiment_id": "retry_cap_0_20s", "separation": 4, "blast_radius_pct": 0.0, "score": 4.0, "selected": True},
        {"experiment_id": "shed_50_20s", "separation": 4, "blast_radius_pct": 50.0, "score": -1.0, "selected": False},
    ]})

    assert "retry_cap_0_20s: separation 4, blast radius 0.0%, score 4.0 — selected" in detail
    assert "shed_50_20s" in detail and "— selected" not in detail.splitlines()[1]
    assert "planner" not in detail  # internal routing flag, not evidence


def test_detail_is_bounded_and_empty_payloads_stay_empty():
    assert audit_detail({}) == ""
    assert len(audit_detail({"summary": "x" * 9000})) <= 4000


def _recording_with_reference(tmp_path: Path, event_id: str) -> Recording:
    recording = new_recording(["probe"], Protocol(), False)
    run: Run = recording.runs[0]
    run.events = [
        Event(at_s=1, kind="triage", title="ambiguous", reference=f"c4:{event_id}"),
        Event(at_s=2, kind="conclusion", title="already detailed", detail="kept", reference=f"c4:{event_id}"),
        Event(at_s=3, kind="observation", title="no audit reference"),
    ]
    work = tmp_path / run.id
    work.mkdir(parents=True)
    (work / "audit.jsonl").write_text(json.dumps({"event_id": event_id, "payload": TRIAGE}) + "\n")
    return recording


def test_backfill_fills_only_empty_detail_and_records_that_it_did(tmp_path):
    recording = _recording_with_reference(tmp_path, "abc123")

    filled, considered = enrich(recording, tmp_path)

    events = recording.runs[0].events
    assert (filled, considered) == (1, 1)  # the detailed and unreferenced events are left alone
    assert "Both stories fit the telemetry." in events[0].detail
    assert events[1].detail == "kept" and events[2].detail == ""
    assert NOTE in recording.protocol.notes


def test_backfill_is_idempotent_and_survives_a_missing_audit_log(tmp_path):
    recording = _recording_with_reference(tmp_path, "abc123")
    enrich(recording, tmp_path)
    before = [event.detail for event in recording.runs[0].events]

    assert enrich(recording, tmp_path) == (0, 0)
    assert [event.detail for event in recording.runs[0].events] == before
    assert recording.protocol.notes.count(NOTE) == 1
    assert enrich(_recording_with_reference(tmp_path, "abc123"), tmp_path / "absent") == (0, 1)
