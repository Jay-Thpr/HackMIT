from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from faultline_contracts import AuditEvent
from faultline_product.api import create_app
from faultline_product.ui_scenario import scenario_from_incident

AUDIT = Path(__file__).parent / "data" / "audit-demo-storm-2.jsonl"
KINDS = {"baseline", "detect", "reason", "clone", "lifecycle", "action", "observe", "undo", "verdict", "archive"}
ACTORS = {"model", "math", "adapter", "investigator-a", "investigator-b", "orchestrator"}


def _events():
    return [AuditEvent.model_validate_json(line) for line in AUDIT.read_text().splitlines() if line.strip()]


def test_scenario_from_a_real_run_matches_the_ui_model():
    sc = scenario_from_incident("demo-storm-2", _events())

    assert sc["id"] == "demo-storm-2" and sc["duration"] > 900
    assert [n["id"] for n in sc["topology"]["nodes"]] == ["db", "gateway", "orders", "payments"]
    assert {h["id"] for h in sc["hypotheses"]} == {"H_db", "H_meta"}
    events = sc["events"]
    assert events == sorted(events, key=lambda e: (e["at"], e["sequence"]))
    assert {e["kind"] for e in events} <= KINDS and {e["actor"] for e in events} <= ACTORS
    assert events[0]["kind"] == "baseline" and events[0]["at"] == 0
    assert next(e for e in events if e["kind"] == "detect")["at"] == 12

    # every production lever becomes an action with a TTL and is later undone by action_id
    actions = [e for e in events if e["kind"] == "action" and e["environmentId"] == "production"]
    undos = {e["undoId"] for e in events if e["kind"] == "undo"}
    assert len(actions) == 4 and all(a["action"]["ttl"] > 0 for a in actions)
    assert {a["action"]["id"] for a in actions} <= undos

    # clones appear as environments before the production probe, with test columns
    clones = [e for e in events if e["kind"] == "clone"]
    # H_meta's investigation aborted before its clone was recorded: no environment is invented for it
    assert {e["environmentId"] for e in clones} == {"clone-h_db", "verify-1", "verify-2"}
    assert any(e["title"].startswith("H_meta: not investigated") for e in events)
    assert events[-1]["incident"] == "complete"  # the report event closes the incident lifecycle
    first_prod_action = min(a["at"] for a in actions)
    assert all(c["at"] < first_prod_action for c in clones if c["environmentId"].startswith("clone-"))
    tests = [e["testResult"] for e in events if e.get("testResult")]
    assert {t["checkId"] for t in tests} == {"reproduce", "recover", "replay"}
    assert all(t["passed"] for t in tests if t["checkId"] == "replay")

    verdict = next(e for e in events if e["kind"] == "verdict")
    assert verdict["actor"] == "math" and "H_meta confirmed" in verdict["title"] and "z " in verdict["result"]

    # every clone has a recorded lifecycle: starting -> ready -> ... -> destroying -> archive
    for env in {c["environmentId"] for c in clones}:
        mine = [e for e in events if e["environmentId"] == env]
        kinds = [(e["kind"], e.get("lifecycle")) for e in mine]
        assert kinds[0] == ("clone", "starting") and ("lifecycle", "ready") in kinds
        assert kinds[-2:] == [("lifecycle", "destroying"), ("archive", None)]
        assert mine[-1]["at"] == mine[-2]["at"] + 3
    # investigation clones are gone before production is touched; the verify clone after the verdict
    archived = {e["environmentId"]: e["at"] for e in events if e["kind"] == "archive"}
    assert all(archived[c] <= first_prod_action + 3 for c in archived if c.startswith("clone-"))
    assert all(archived[c] > verdict["at"] for c in archived if c.startswith("verify-"))
    reason = next(e for e in events if e["kind"] == "reason")
    assert reason["actor"] == "model"


def test_scenario_without_windows_reads_unknown_not_zero():
    sc = scenario_from_incident("demo-storm-2", _events())
    assert all(r == {"health": "unknown"} for r in sc["baseline"].values())
    detect = next(e for e in sc["events"] if e["kind"] == "detect")
    assert all(r["health"] == "unknown" for r in detect["readings"].values())


@pytest.mark.parametrize("diagnosis,confirmed", [("H_meta", True), ("H_db", True), ("H_db", False), ("none_of_the_above", False)])
def test_scenario_preserves_structured_verdict(diagnosis, confirmed):
    source = next(e for e in _events() if e.kind.value == "verdict")
    event = source.model_copy(update={"payload": {**source.payload, "diagnosis": diagnosis, "confirmed": confirmed}})
    scenario = scenario_from_incident(event.incident_id, [event])
    verdict = next(e for e in scenario["events"] if e["kind"] == "verdict")
    assert verdict["diagnosis"] == diagnosis
    assert verdict["confirmed"] is confirmed
    assert scenario["incidentTitle"] == event.summary


@pytest.mark.parametrize("status", ["active", "undone", "expired", None])
def test_scenario_preserves_release_status_and_action_identity(status):
    source = next(e for e in _events() if e.kind.value == "action_undo")
    payload = {k: v for k, v in source.payload.items() if k not in ("status", "action_id")}
    if status is not None:
        payload["status"] = status
    event = source.model_copy(update={"payload": payload})
    scenario = scenario_from_incident(event.incident_id, [event])
    undo = next(e for e in scenario["events"] if e["kind"] == "undo")
    assert undo["undoId"] == source.action_id
    assert undo["undoStatus"] == (status or "unknown")
    if status not in ("undone", "expired"):
        assert "not confirmed" in undo["title"].lower()


def test_unknown_incident_raises():
    with pytest.raises(ValueError):
        scenario_from_incident("nope", _events())


def test_api_serves_incidents_events_and_scenario(tmp_path):
    client = TestClient(create_app([AUDIT, tmp_path / "missing.jsonl"]))
    assert client.get("/api/health").json()["elasticsearch"] is False
    incidents = client.get("/api/incidents").json()
    assert incidents[0]["id"] == "demo-storm-2" and incidents[0]["diagnosis"] == "H_meta"
    assert incidents[0]["outcome"] == "incident report ready"
    assert len(client.get("/api/incidents/demo-storm-2/events").json()) == 24
    scenario = client.get("/api/incidents/demo-storm-2/scenario").json()
    assert scenario["hypotheses"] and scenario["events"]
    assert client.get("/api/incidents/demo-storm-2/series").json() == []
    assert client.get("/api/incidents/nope/scenario").status_code == 404


def test_scenario_marks_completion_and_now():
    from datetime import timedelta

    events = _events()
    done = scenario_from_incident("demo-storm-2", events)
    assert done["complete"] is True and done["now"] == done["duration"]
    partial = scenario_from_incident("demo-storm-2", events[:12], now=events[11].ts + timedelta(seconds=300))
    assert partial["complete"] is False and partial["report"]["outcome"] == "in progress"
    stale = scenario_from_incident("demo-storm-2", events[:12], now=events[11].ts + timedelta(hours=2))
    assert stale["complete"] is True and stale["report"]["outcome"].startswith("abandoned")
    assert done["report"]["diagnosis"] == "H_meta" and done["report"]["canary"] == "passed" and done["report"]["patchRevision"] == 1
    assert partial["duration"] == partial["now"] > max(e["at"] for e in partial["events"])


def test_stream_emits_scenario_frames_as_the_audit_grows_and_done_at_report(tmp_path):
    import asyncio
    import json

    from faultline_product.api import IncidentReader, scenario_updates

    lines = AUDIT.read_text().splitlines()
    path = tmp_path / "audit.jsonl"
    path.write_text("\n".join(lines[:5]) + "\n")
    reader = IncidentReader([path], None)
    appended = {"n": 5}

    async def sleep(_s):  # each poll: append a chunk of the real log
        nxt = min(len(lines), appended["n"] + 8)
        path.write_text("\n".join(lines[:nxt]) + "\n")
        appended["n"] = nxt

    recorded_end = _events()[-1].ts  # the log is hours old: pretend "now" is when it was written

    async def collect():
        frames = []
        async for frame in scenario_updates(reader, "demo-storm-2", poll_s=0, sleep=sleep, clock=lambda: recorded_end):
            frames.append(frame)
        return frames

    frames = asyncio.run(collect())
    scenarios = [json.loads(f.split("data: ", 1)[1]) for f in frames if f.startswith("event: scenario")]
    assert len(scenarios) >= 3 and frames[-1].startswith("event: done")
    counts = [len(s["events"]) for s in scenarios]
    assert counts == sorted(counts) and scenarios[-1]["complete"] and not scenarios[0]["complete"]


def test_stream_endpoint_is_sse(tmp_path):
    client = TestClient(create_app([AUDIT]))
    with client.stream("GET", "/api/incidents/demo-storm-2/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = ""
        for chunk in response.iter_text():
            body += chunk
            if "event: done" in body:
                break
    assert body.startswith("event: scenario\ndata: {") and body.rstrip().endswith("event: done\ndata: {}")
