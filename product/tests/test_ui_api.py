from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from faultline_contracts import AuditEvent
from faultline_product.api import create_app
from faultline_product.ui_scenario import scenario_from_incident

AUDIT = Path(__file__).parent / "data" / "audit-demo-storm-2.jsonl"
KINDS = {"baseline", "detect", "reason", "clone", "action", "observe", "undo", "verdict", "archive"}
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
    assert {e["environmentId"] for e in clones} == {"clone-h_db", "clone-h_meta", "verify-1", "verify-2"}
    first_prod_action = min(a["at"] for a in actions)
    assert all(c["at"] < first_prod_action for c in clones if c["environmentId"].startswith("clone-"))
    tests = [e["testResult"] for e in events if e.get("testResult")]
    assert {t["checkId"] for t in tests} == {"reproduce", "recover", "replay"}
    assert all(t["passed"] for t in tests if t["checkId"] == "replay")

    verdict = next(e for e in events if e["kind"] == "verdict")
    assert verdict["actor"] == "math" and "H_meta confirmed" in verdict["title"] and "z " in verdict["result"]
    reason = next(e for e in events if e["kind"] == "reason")
    assert reason["actor"] == "model"


def test_scenario_without_windows_reads_unknown_not_zero():
    sc = scenario_from_incident("demo-storm-2", _events())
    assert all(r == {"health": "unknown"} for r in sc["baseline"].values())
    detect = next(e for e in sc["events"] if e["kind"] == "detect")
    assert all(r["health"] == "unknown" for r in detect["readings"].values())


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
