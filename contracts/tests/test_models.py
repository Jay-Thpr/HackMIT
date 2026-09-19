import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from faultline_contracts import (
    NONE_OF_THE_ABOVE,
    AuditEvent,
    Experiment,
    Fingerprint,
    JsonlSink,
    LeverSpec,
    TriageResult,
    Verdict,
    experiment_windows,
    is_valid_metric_key,
    standard_blast_radius,
)
from faultline_contracts.fault import FaultState
from faultline_contracts.openai_schema import triage_response_format

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"
HERO_EXPERIMENTS = {"retry_cap_0_20s", "db_failover_30s"}

SINGLE = {
    "fingerprint_healthy.json": Fingerprint,
    "fingerprint_storm.json": Fingerprint,
    "fingerprint_degraded_db.json": Fingerprint,
    "triage_hero.json": TriageResult,
    "verdict_storm.json": Verdict,
    "fault_state_storm.json": FaultState,
}
LISTS = {
    "series_storm_experiment.json": Fingerprint,
    "series_degraded_db_experiment.json": Fingerprint,
    "catalog.json": LeverSpec,
    "experiments.json": Experiment,
}


def load(name):
    return json.loads((FIX / name).read_text())


def audit_events():
    return [AuditEvent.model_validate_json(l) for l in (FIX / "audit_hero.jsonl").read_text().splitlines() if l]


def storm_keys():
    return set(Fingerprint.model_validate(load("fingerprint_storm.json")).metrics())


def hero():
    return TriageResult.model_validate(load("triage_hero.json"))


@pytest.mark.parametrize("name,model", SINGLE.items())
def test_single_fixture_roundtrip(name, model):
    obj = model.model_validate(load(name))
    assert model.model_validate_json(obj.model_dump_json()) == obj


@pytest.mark.parametrize("name,model", LISTS.items())
def test_list_fixture_roundtrip(name, model):
    ta = TypeAdapter(list[model])
    objs = ta.validate_python(load(name))
    assert objs
    assert ta.validate_json(ta.dump_json(objs)) == objs


def test_audit_fixture_roundtrip():
    evs = audit_events()
    assert len(evs) == 11
    for e in evs:
        assert AuditEvent.model_validate_json(e.model_dump_json()) == e


def test_series_shape():
    for name in ("series_storm_experiment.json", "series_degraded_db_experiment.json"):
        fps = TypeAdapter(list[Fingerprint]).validate_python(load(name))
        assert len(fps) == 34
        for a, b in zip(fps, fps[1:]):
            assert b.window_start == a.window_end
            assert (a.window_end - a.window_start).total_seconds() == 5


@pytest.mark.parametrize("name", ["fingerprint_healthy.json", "fingerprint_storm.json", "fingerprint_degraded_db.json"])
def test_fingerprint_metric_keys_valid(name):
    m = Fingerprint.model_validate(load(name)).metrics()
    assert m and all(is_valid_metric_key(k) for k in m)
    assert "db.query_p50_ms" in m and "edge.payments.fraud_check.qps" in m and "slo.checkout.value" in m


def test_triage_hero_has_no_problems():
    assert hero().problems(known_metrics=storm_keys(), experiment_ids=HERO_EXPERIMENTS) == []


def test_problems_catches_errors():
    keys = storm_keys()
    t = hero()
    bad = t.model_copy(deep=True)
    bad.predictions[0].during[0].metric = "db.nonsense"
    assert any("unknown metric" in p for p in bad.problems(known_metrics=keys))

    bad = t.model_copy(deep=True)
    bad.predictions[0].during[0].metric = "svc.nosuchsvc.qps"  # well-formed but not in live fingerprint
    assert any("unknown metric" in p for p in bad.problems(known_metrics=keys))

    bad = t.model_copy(deep=True)
    bad.predictions[0].hypothesis_id = "H_ghost"
    assert any("unknown hypothesis" in p for p in bad.problems())

    bad = t.model_copy(deep=True)
    bad.hypotheses[0].id = NONE_OF_THE_ABOVE
    assert any("reserved" in p for p in bad.problems())

    assert any("unknown experiment" in p for p in t.problems(experiment_ids={"retry_cap_0_20s"}))


def test_extra_fields_rejected():
    d = load("fingerprint_storm.json")
    d["world"] = "x"
    with pytest.raises(ValidationError):
        Fingerprint.model_validate(d)
    d = load("triage_hero.json")
    d["hypotheses"][0]["confidence"] = 0.9
    with pytest.raises(ValidationError):
        TriageResult.model_validate(d)


def test_experiment_windows_from_audit():
    wins = experiment_windows(audit_events())
    assert len(wins) == 1
    w = wins[0]
    assert w.experiment_id == "retry_cap_0_20s"
    assert w.start == datetime(2026, 9, 19, 15, 2, 0, tzinfo=timezone.utc)
    assert w.release == w.start + timedelta(seconds=20)


def test_jsonl_sink_roundtrip(tmp_path):
    sink = JsonlSink(tmp_path / "sub" / "audit.jsonl")
    evs = audit_events()
    for e in evs:
        sink.write(e)
    sink.write(evs[0].model_copy(update={"incident_id": "other", "event_id": "x"}))
    assert sink.query(evs[0].incident_id) == evs
    assert len(sink.query("other")) == 1
    assert sink.query("missing") == []


def test_standard_blast_radius():
    assert standard_blast_radius("retry_cap", {"max_retries": 0}) == 0.0
    assert standard_blast_radius("shed", {"fraction": 0.5}) == 50.0
    assert standard_blast_radius("db_failover", {}) == 1.0
    assert standard_blast_radius("canary_weight", {"v2_weight": 0.05}) == pytest.approx(5.0)
    exps = {e["id"]: e["blast_radius_pct"] for e in load("experiments.json")}
    assert exps == {"retry_cap_0_20s": 0.0, "shed_10_20s": 10.0, "shed_50_20s": 50.0, "db_failover_30s": 1.0}


def _walk(node):
    if isinstance(node, dict):
        yield node
        for k, v in node.items():
            if k in ("properties", "$defs"):
                for sub in v.values():
                    yield from _walk(sub)
            else:
                yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_openai_schema_strict():
    rf = triage_response_format()
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    schema = rf["json_schema"]["schema"]
    objects = [n for n in _walk(schema) if n.get("type") == "object"]
    assert len(objects) >= 5
    for o in objects:
        assert o["additionalProperties"] is False
        assert sorted(o["required"]) == sorted(o["properties"])
    for n in _walk(schema):
        assert "title" not in n and "default" not in n


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_schema_files_up_to_date(tmp_path):
    exp = _load_script("export_schemas")
    for p in exp.export_all(tmp_path):
        committed = ROOT / "schema" / p.name
        assert committed.exists(), f"missing {committed}; run scripts/export_schemas.py"
        assert committed.read_text() == p.read_text(), f"{p.name} stale; run scripts/export_schemas.py"
