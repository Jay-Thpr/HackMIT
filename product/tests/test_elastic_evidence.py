import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from faultline_brain import AgenticCloneInvestigator
from faultline_brain.agent_builder import AgentBuilderClient
from faultline_contracts import Actor, AuditEvent, EventKind, JsonlSink, Stage
from faultline_contracts.clone import CloneSpec, LAB_CATALOG
from faultline_contracts.fingerprint import Fingerprint

from test_investigate import FakeLab

from faultline_product.adapters import LiveBrain
from faultline_product.adapters.evidence import (
    EvidenceInvestigatorAgent,
    evidence_references,
    production_evidence,
)
from faultline_product.cli import build_parser, main
from faultline_product.fixtures import load_fixture
from faultline_product.report import render_report

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures"

URL = "https://kibana.example.com"
KEY = "test-key"


class _FakeReader:
    def __init__(self, context_result=None, error=None):
        self.context_calls = []
        self.similar_calls = []
        self.context_result = context_result
        self.error = error

    def context(self, incident_id, start, end, *, clone_id=None):
        self.context_calls.append({"incident_id": incident_id, "start": start, "end": end, "clone_id": clone_id})
        if self.error is not None:
            raise self.error
        if self.context_result is not None:
            return self.context_result
        return {
            "source": "primary_elasticsearch",
            "status": "ok",
            "scope": {"incident_id": incident_id, "environment": "clone" if clone_id else "production", "clone_id": clone_id},
            "timeline": {"status": "ok", "items": [{"reference": "c1:w1"}], "truncated": False, "rejected": 0, "lag_s": 5.0},
            "audit": {"status": "ok", "items": [{"reference": "c4:e1"}], "truncated": False, "rejected": 0},
        }

    def similar_incidents(self, fingerprint, *, incident_id, before):
        self.similar_calls.append({"incident_id": incident_id, "before": before})
        if self.error is not None:
            raise self.error
        return {"status": "empty", "items": [], "scope": {"excluded_incident_id": incident_id}}


def _builder(bodies, payload):
    return AgentBuilderClient(
        URL, KEY, role="triage",
        request=lambda body: bodies.append(body) or payload,
    )


def _draft():
    draft = json.loads((FIXTURES / "triage_hero.json").read_text())
    for key in ("schema_version", "incident_id", "created_at"):
        draft.pop(key)
    return json.dumps(draft)


def test_production_evidence_scope_and_history_cutoff():
    bundle = load_fixture("storm")
    fingerprint = bundle.telemetry.first_breach()
    reader = _FakeReader()

    context = production_evidence(reader, "inc-1", fingerprint)

    call = reader.context_calls[0]
    assert call["incident_id"] == "inc-1"
    assert call["clone_id"] is None
    assert call["end"] == fingerprint.window_end
    assert call["start"] == fingerprint.window_end - timedelta(seconds=120)
    assert reader.similar_calls[0]["before"] == call["start"]
    assert context["similar_incidents"]["status"] == "empty"
    assert evidence_references(context) == ["c1:w1", "c4:e1"]


def test_reader_failure_returns_unavailable_not_exception():
    bundle = load_fixture("storm")
    reader = _FakeReader(error=RuntimeError("boom"))
    context = production_evidence(reader, "inc-1", bundle.telemetry.first_breach())
    assert context["status"] == "unavailable"
    assert context["reason"] == "RuntimeError"
    assert context["source"] == "primary_elasticsearch"


def test_live_brain_loads_evidence_once_into_builder_context():
    bundle = load_fixture("storm")
    fingerprint = bundle.telemetry.first_breach()
    reader = _FakeReader()
    bodies = []
    events = []
    brain = LiveBrain(
        bundle.experiments,
        client=_builder(bodies, {"status": "completed", "response": {"message": _draft()}}),
        provider="agent_builder",
        triage_fallback=bundle.triage,
        provider_sink=events.append,
        evidence_reader=reader,
    )

    result = brain.triage("inc-ev", fingerprint)

    assert result.incident_id == "inc-ev"
    assert len(reader.context_calls) == 1
    sent = json.loads(bodies[0]["input"])
    assert sent["context"]["scope"]["incident_id"] == "inc-ev"
    loaded = [e for e in events if e["status"] == "evidence_loaded"]
    assert loaded[0]["evidence_status"] == "ok"
    assert "c1:w1" in loaded[0]["references"]


def test_no_reader_means_no_retrieval_and_no_context():
    bundle = load_fixture("storm")
    bodies = []
    events = []
    brain = LiveBrain(
        bundle.experiments,
        client=_builder(bodies, {"status": "completed", "response": {"message": _draft()}}),
        provider="agent_builder",
        triage_fallback=bundle.triage,
        provider_sink=events.append,
    )

    brain.triage("inc-off", bundle.telemetry.first_breach())

    assert "context" not in json.loads(bodies[0]["input"])
    assert all(e["status"] != "evidence_loaded" for e in events)


def test_unavailable_reader_still_lets_snapshot_triage_run():
    bundle = load_fixture("storm")
    bodies = []
    brain = LiveBrain(
        bundle.experiments,
        client=_builder(bodies, {"status": "completed", "response": {"message": _draft()}}),
        provider="agent_builder",
        triage_fallback=bundle.triage,
        evidence_reader=_FakeReader(error=RuntimeError("es down")),
    )

    result = brain.triage("inc-down", bundle.telemetry.first_breach())

    assert result.incident_id == "inc-down"
    sent = json.loads(bodies[0]["input"])
    assert sent["context"]["status"] == "unavailable"


_PROPOSAL = json.dumps({
    "action": "db_latency",
    "params": {"extra_ms": 800, "capacity_qps": None, "service": None, "cpus": None, "max_retries": None, "timeout_ms": None},
    "ttl_s": 20,
    "observe_after_s": 15,
    "predicted": [{"metric": "db.qps", "direction": "up"}],
    "rationale": "inject latency",
    "stop": False,
    "stop_reason": "",
})


def _clone(clone_id, created_at):
    return SimpleNamespace(clone_id=clone_id, created_at=created_at)


def test_evidence_investigator_binds_own_clone_context():
    fingerprint = Fingerprint.model_validate_json((FIXTURES / "fingerprint_storm.json").read_text())
    healthy = [Fingerprint.model_validate_json((FIXTURES / "fingerprint_healthy.json").read_text())]
    bodies = []
    base = AgentBuilderClient(
        URL, KEY, role="investigator",
        request=lambda body: bodies.append(body) or {"status": "completed", "response": {"message": _PROPOSAL}},
    )
    clock = lambda: datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
    reader = _FakeReader()
    hypothesis = SimpleNamespace(id="H_meta", label="storm", description="retry storm")
    clone_a = _clone("clone-a", datetime(2026, 9, 20, 11, 59, 0, tzinfo=UTC))
    clone_b = _clone("clone-b", datetime(2026, 9, 20, 11, 50, 0, tzinfo=UTC))

    agent_a = EvidenceInvestigatorAgent(base, reader, "inc-1", "H_meta", clone_a, model="m", clock=clock)
    agent_b = EvidenceInvestigatorAgent(base, reader, "inc-1", "H_db", clone_b, model="m", clock=clock)
    proposal_a = agent_a.propose(hypothesis, LAB_CATALOG, fingerprint, healthy, [], 3)
    agent_b.propose(hypothesis, LAB_CATALOG, fingerprint, healthy, [], 3)

    assert proposal_a.action == "db_latency"
    context_a = json.loads(bodies[0]["input"])["context"]
    context_b = json.loads(bodies[1]["input"])["context"]
    assert context_a["clone_id"] == "clone-a" and context_b["clone_id"] == "clone-b"
    assert context_a["hypothesis_id"] == "H_meta" and context_b["hypothesis_id"] == "H_db"
    clone_calls = [c for c in reader.context_calls if c["clone_id"]]
    assert clone_calls[0]["clone_id"] == "clone-a"
    assert clone_calls[0]["start"] == clone_a.created_at
    assert clone_calls[1]["start"] == clock() - timedelta(seconds=120)


def test_agent_factory_exception_resets_and_destroys_clone():
    fingerprint = Fingerprint.model_validate_json((FIXTURES / "fingerprint_storm.json").read_text())
    lab = FakeLab()

    def broken_factory(clone):
        raise ValueError("factory boom")

    investigator = AgenticCloneInvestigator(
        lab, lambda clone: fingerprint, object(),
        agent_for_clone=broken_factory,
    )

    with pytest.raises(ValueError, match="factory boom"):
        investigator.investigate(
            SimpleNamespace(id="H_meta"), CloneSpec(name="c"), fingerprint, [fingerprint]
        )

    clone_id = "c-1"
    assert lab.events.count(f"reset:{clone_id}") == 1
    assert lab.events.count(f"destroy:{clone_id}") == 1


def test_evidence_investigator_emits_metadata_not_content():
    fingerprint = Fingerprint.model_validate_json((FIXTURES / "fingerprint_storm.json").read_text())
    healthy = [Fingerprint.model_validate_json((FIXTURES / "fingerprint_healthy.json").read_text())]
    reader = _FakeReader()
    reader.context_result = {
        "status": "partial",
        "scope": {"incident_id": "inc-1"},
        "timeline": {"status": "partial", "items": [{"reference": "c1:zz"}], "truncated": True},
        "audit": {"status": "ok", "items": [{"reference": "c4:zz"}]},
    }
    events = []
    base = AgentBuilderClient(
        URL, KEY, role="investigator",
        request=lambda body: {"status": "completed", "response": {"message": _PROPOSAL}},
    )
    clone = _clone("clone-x", datetime(2026, 9, 20, 11, 0, 0, tzinfo=UTC))
    agent = EvidenceInvestigatorAgent(
        base, reader, "inc-1", "H_meta", clone, model="m",
        clock=lambda: datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
        provenance_sink=events.append,
    )

    agent.propose(SimpleNamespace(id="H_meta", label="s", description="d"), LAB_CATALOG, fingerprint, healthy, [], 3)

    loaded = next(e for e in events if e["status"] == "evidence_loaded")
    assert loaded["hypothesis_id"] == "H_meta" and loaded["clone_id"] == "clone-x"
    assert loaded["production"]["evidence_status"] == "partial"
    assert loaded["production"]["references"] == ["c1:zz", "c4:zz"]
    assert loaded["clone"]["evidence_status"] == "partial"
    assert "items" not in json.dumps(loaded)


def test_investigation_agent_factory_only_with_reader(monkeypatch):
    from faultline_brain.agent_builder import FallbackInvestigatorAgent
    from faultline_product.cli import _investigation

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = build_parser().parse_args([
        "watch", "--telemetry", "sandbox", "--levers", "sandbox",
        "--lab-url", "http://127.0.0.1:9910",
        "--reasoning-provider", "agent-builder",
    ])
    proposal = AgentBuilderClient(URL, KEY, role="investigator", request=lambda b: {})

    with_reader = _investigation(args, proposal_client=proposal, provider_sink=None, evidence_reader=_FakeReader())
    assert callable(with_reader._agent_factory)
    made = with_reader._agent_factory("inc-1", SimpleNamespace(id="H_meta"), _clone("c1", datetime.now(UTC)), None)
    assert isinstance(made, FallbackInvestigatorAgent)
    assert isinstance(made._primary, EvidenceInvestigatorAgent)

    without_reader = _investigation(args, proposal_client=proposal, provider_sink=None)
    assert without_reader._agent_factory is None
    assert isinstance(without_reader._agent, FallbackInvestigatorAgent)


def _write_audit(path, incident_id):
    sink = JsonlSink(path)
    sink.write(AuditEvent(
        incident_id=incident_id, stage=Stage.triage, kind=EventKind.triage,
        actor=Actor.llm, summary="ambiguous", payload={"hypotheses": ["H_meta"]},
    ))
    sink.write(AuditEvent(
        incident_id=incident_id, stage=Stage.experiment, kind=EventKind.verdict,
        actor=Actor.math, summary="verdict", payload={"diagnosis": "H_meta"},
    ))
    return sink


def test_report_uses_most_recent_verdict_and_patch(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlSink(path)
    base = datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC)
    sink.write(AuditEvent(
        incident_id="inc-r", ts=base, stage=Stage.experiment, kind=EventKind.verdict,
        actor=Actor.math, summary="v1", payload={"diagnosis": "H_db"},
    ))
    sink.write(AuditEvent(
        incident_id="inc-r", ts=base + timedelta(seconds=10), stage=Stage.patch,
        kind=EventKind.patch_opened, actor=Actor.orchestrator, summary="p1",
        payload={"reference": "pull/1"},
    ))
    sink.write(AuditEvent(
        incident_id="inc-r", ts=base + timedelta(seconds=20), stage=Stage.experiment,
        kind=EventKind.verdict, actor=Actor.math, summary="v2", payload={"diagnosis": "H_meta"},
    ))
    sink.write(AuditEvent(
        incident_id="inc-r", ts=base + timedelta(seconds=30), stage=Stage.patch,
        kind=EventKind.patch_opened, actor=Actor.orchestrator, summary="p2",
        payload={"reference": "pull/2"},
    ))

    text = render_report(sink, "inc-r")

    assert "Diagnosis: H_meta" in text
    assert "Patch: pull/2" in text
    assert "  v1" in text and "  p1" in text


def test_report_default_output_unchanged_without_reader(tmp_path):
    audit = _write_audit(tmp_path / "audit.jsonl", "inc-r")
    text = render_report(audit, "inc-r")
    assert "Elastic evidence" not in text
    assert "Diagnosis: H_meta" in text


def test_report_appends_scoped_evidence_summary(tmp_path):
    audit = _write_audit(tmp_path / "audit.jsonl", "inc-r")
    reader = _FakeReader()

    text = render_report(audit, "inc-r", evidence_reader=reader)

    assert "Elastic evidence: ok" in text
    assert "windows: 1 returned (RETURNED count" in text
    assert "lag_s=5" in text
    assert "c1:w1" in text and "c4:e1" in text
    call = reader.context_calls[0]
    events = audit.query("inc-r")
    assert call["end"] == max(e.ts for e in events) + timedelta(microseconds=1)
    assert call["start"] >= call["end"] - timedelta(hours=6)


def test_report_evidence_unavailable_preserves_report(tmp_path):
    audit = _write_audit(tmp_path / "audit.jsonl", "inc-r")
    reader = _FakeReader(error=RuntimeError("es down"))

    text = render_report(audit, "inc-r", evidence_reader=reader)

    assert "Diagnosis: H_meta" in text
    assert "Elastic evidence: unavailable" in text
    assert "RuntimeError" in text


def test_report_explanation_cites_only_known_references(tmp_path):
    audit = _write_audit(tmp_path / "audit.jsonl", "inc-r")
    reader = _FakeReader()
    bodies = []
    explanation = {
        "observations": [{"text": "retry ratio stayed elevated", "references": ["c1:w1"]}],
        "limitations": ["audit coverage partial"],
    }
    client = AgentBuilderClient(
        URL, KEY, role="report",
        request=lambda body: bodies.append(body) or {"status": "completed", "response": {"message": json.dumps(explanation)}},
    )

    text = render_report(audit, "inc-r", evidence_reader=reader, explanation_client=client)

    assert "Agent Builder explanation (not a verdict):" in text
    assert "retry ratio stayed elevated" in text
    assert "c1:w1" in text
    assert json.loads(bodies[0]["input"])["context"]["status"] == "ok"

    bad = {"observations": [{"text": "invented", "references": ["c1:not-present"]}], "limitations": []}
    bad_client = AgentBuilderClient(
        URL, KEY, role="report",
        request=lambda body: {"status": "completed", "response": {"message": json.dumps(bad)}},
    )
    text = render_report(audit, "inc-r", evidence_reader=_FakeReader(), explanation_client=bad_client)
    assert "Agent Builder explanation unavailable (ValueError)" in text
    assert "invented" not in text


def test_cli_report_uses_read_only_primary_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("faultline_product.cli.load_repo_dotenv", lambda *_: None)
    monkeypatch.setenv("FAULTLINE_ELASTICSEARCH_URL", URL)
    monkeypatch.setenv("FAULTLINE_ELASTICSEARCH_API_KEY", KEY)
    audit_path = tmp_path / "audit.jsonl"
    _write_audit(audit_path, "inc-r")

    created = []
    forbidden = []

    class _PlainClient:
        def __init__(self, url, api_key=None, **kwargs):
            created.append((url, api_key is not None))

        def close(self):
            created.append(("closed", False))

    monkeypatch.setattr("faultline_product.cli.HttpElasticsearchClient", _PlainClient)
    monkeypatch.setattr(
        "faultline_product.cli.client_from_env",
        lambda *a, **k: forbidden.append("client_from_env") or None,
    )
    monkeypatch.setattr(
        "faultline_product.cli.ensure_index_templates",
        lambda *a, **k: forbidden.append("ensure_index_templates"),
    )

    result = main(["--audit-log", str(audit_path), "report", "--incident", "inc-r", "--elastic-evidence"])

    assert result == 0
    assert created == [(URL, True), ("closed", False)]
    assert forbidden == []


def test_cli_explain_requires_elastic_evidence(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("faultline_product.cli.load_repo_dotenv", lambda *_: None)
    audit_path = tmp_path / "audit.jsonl"
    _write_audit(audit_path, "inc-r")

    result = main(["--audit-log", str(audit_path), "report", "--incident", "inc-r", "--explain"])

    assert result == 2
    assert "--explain requires --elastic-evidence" in capsys.readouterr().out
