import json
from pathlib import Path
from types import SimpleNamespace

from faultline_brain.agent_builder import (
    AgentBuilderClient,
    AgentBuilderError,
    FallbackInvestigatorAgent,
)
from faultline_product.adapters import LiveBrain, build_live_brain
from faultline_product.adapters.investigate import LabInvestigation
from faultline_product.cli import _investigation, build_parser, main
from faultline_product.fixtures import load_fixture

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures"

URL = "https://kibana.example.com"
KEY = "test-key"


def _draft():
    draft = json.loads((FIXTURES / "triage_hero.json").read_text())
    for key in ("schema_version", "incident_id", "created_at"):
        draft.pop(key)
    return json.dumps(draft)


def _builder(payload_or_error):
    def request(body):
        if isinstance(payload_or_error, Exception):
            raise payload_or_error
        return payload_or_error

    return AgentBuilderClient(URL, KEY, role="triage", request=request)


def _valid_payload():
    return {"status": "completed", "response": {"message": _draft()}}


class _DirectClient:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


def test_reasoning_provider_flag_defaults_to_direct():
    args = build_parser().parse_args(["watch"])
    assert args.reasoning_provider == "direct"
    for command in ("watch", "investigate", "experiment --id retry_cap_0_20s"):
        parsed = build_parser().parse_args([*command.split(), "--reasoning-provider", "agent-builder"])
        assert parsed.reasoning_provider == "agent-builder"


def test_agent_builder_requires_live_brain(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("faultline_product.cli.load_repo_dotenv", lambda *_: None)
    result = main([
        "--audit-log", str(tmp_path / "audit.jsonl"),
        "watch", "--reasoning-provider", "agent-builder",
    ])
    assert result == 2
    assert "requires --brain live" in capsys.readouterr().out


def test_build_live_brain_validates_agent_builder_primary():
    bundle = load_fixture("storm")
    events = []
    brain = build_live_brain(
        bundle.experiments,
        api_key=None,
        model="gpt-test",
        triage_fallback=bundle.triage,
        proposal_client=_builder(_valid_payload()),
        provider_sink=events.append,
    )

    result = brain.triage("ab-1", bundle.telemetry.first_breach())

    assert result.incident_id == "ab-1"
    assert brain.last_triage_source == "agent_builder"
    assert events == [{"provider": "agent_builder", "role": "triage", "status": "validated"}]


def test_semantic_rejection_falls_back_to_direct_openai():
    bundle = load_fixture("storm")
    bad = json.loads(_draft())
    bad["predictions"][0]["during"][0]["metric"] = "db.not_real"
    payload = {"status": "completed", "response": {"message": json.dumps(bad)}}
    events = []
    brain = LiveBrain(
        bundle.experiments,
        client=_builder(payload),
        fallback_client=_DirectClient(content=_draft()),
        provider="agent_builder",
        triage_fallback=bundle.triage,
        provider_sink=events.append,
    )

    result = brain.triage("ab-2", bundle.telemetry.first_breach())

    assert result.incident_id == "ab-2"
    assert brain.last_triage_source == "openai"
    assert "fallback after" in brain.last_triage_note
    assert [e["status"] for e in events] == ["rejected", "validated"]


def test_transport_failure_falls_back_then_fixture_when_all_rejected():
    bundle = load_fixture("storm")
    brain = LiveBrain(
        bundle.experiments,
        client=_builder(AgentBuilderError("down")),
        fallback_client=_DirectClient(error=RuntimeError("openai down")),
        provider="agent_builder",
        triage_fallback=bundle.triage,
    )

    result = brain.triage("ab-3", bundle.telemetry.first_breach())

    assert result.incident_id == "ab-3"
    assert brain.last_triage_source == "fallback"
    assert "AgentBuilderError" in brain.last_triage_note


def test_cli_persists_triage_source_and_provider_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("faultline_product.cli.load_repo_dotenv", lambda *_: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("KIBANA_URL", URL)
    monkeypatch.setenv("ELASTIC_AGENT_BUILDER_API_KEY", KEY)
    monkeypatch.setattr(
        AgentBuilderClient, "from_env",
        classmethod(lambda cls, *, role, env=None, provenance_sink=None: AgentBuilderClient(
            URL, KEY, role=role, request=lambda body: _valid_payload(),
            provenance_sink=provenance_sink,
        )),
    )
    audit_log = tmp_path / "audit.jsonl"

    result = main([
        "--audit-log", str(audit_log),
        "watch", "--brain", "live", "--reasoning-provider", "agent-builder",
        "--incident", "ab-cli",
    ])

    assert result == 0
    events = [json.loads(line) for line in audit_log.read_text().splitlines()]
    triage = next(e for e in events if e["stage"] == 3 and e["kind"] == "triage" and not e["payload"].get("reasoning_provider"))
    assert triage["payload"]["source"] == "agent_builder"
    provider_events = [e for e in events if e["payload"].get("reasoning_provider")]
    assert any(
        e["payload"]["provider"] == "agent_builder" and e["payload"]["status"] == "validated"
        for e in provider_events
    )


def test_investigation_wraps_builder_client_without_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = build_parser().parse_args([
        "watch", "--telemetry", "sandbox", "--levers", "sandbox",
        "--lab-url", "http://127.0.0.1:9910",
        "--reasoning-provider", "agent-builder",
    ])
    proposal = AgentBuilderClient(URL, KEY, role="investigator", request=lambda b: {})

    investigation = _investigation(args, proposal_client=proposal, provider_sink=lambda e: None)

    assert isinstance(investigation, LabInvestigation)
    assert isinstance(investigation._agent, FallbackInvestigatorAgent)
    assert investigation._agent._fallback is None


def test_investigation_direct_mode_unchanged_without_key(monkeypatch):
    from faultline_brain import SeedInvestigator

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = build_parser().parse_args([
        "watch", "--telemetry", "sandbox", "--levers", "sandbox",
        "--lab-url", "http://127.0.0.1:9910",
    ])

    investigation = _investigation(args)

    assert isinstance(investigation._agent, SeedInvestigator)


def test_elastic_evidence_requires_agent_builder(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("faultline_product.cli.load_repo_dotenv", lambda *_: None)
    result = main([
        "--audit-log", str(tmp_path / "audit.jsonl"),
        "watch", "--brain", "live", "--elastic-evidence",
        "--incident", "ev-flag",
    ])
    assert result == 2
    assert "requires --reasoning-provider agent-builder" in capsys.readouterr().out


def test_builder_primary_works_without_openai_package(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "openai", None)
    monkeypatch.setenv("OPENAI_API_KEY", "present-but-sdk-missing")
    bundle = load_fixture("storm")
    events = []
    brain = build_live_brain(
        bundle.experiments,
        api_key="present-but-sdk-missing",
        model="gpt-test",
        triage_fallback=bundle.triage,
        proposal_client=_builder(_valid_payload()),
        provider_sink=events.append,
    )

    assert brain._fallback_client is None
    assert {"provider": "openai", "role": "triage", "status": "unavailable", "reason": "ImportError"} in events
    result = brain.triage("no-sdk", bundle.telemetry.first_breach())
    assert result.incident_id == "no-sdk"
    assert brain.last_triage_source == "agent_builder"


def test_fixture_fallback_emits_fixture_provider_status():
    bundle = load_fixture("storm")
    events = []
    brain = LiveBrain(
        bundle.experiments,
        client=_builder(AgentBuilderError("down")),
        fallback_client=_DirectClient(error=RuntimeError("openai down")),
        provider="agent_builder",
        triage_fallback=bundle.triage,
        provider_sink=events.append,
    )

    brain.triage("fx", bundle.telemetry.first_breach())

    assert events[-1] == {"provider": "fixture", "role": "triage", "status": "fallback"}
