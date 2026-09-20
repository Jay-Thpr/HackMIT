import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from faultline_contracts.fingerprint import Fingerprint
from faultline_contracts.levers import Experiment

from faultline_brain.agent_builder import (
    AgentBuilderClient,
    AgentBuilderError,
    FallbackInvestigatorAgent,
    _NoRedirect,
)
from faultline_brain.elastic_investigation import (
    EVIDENCE_CONTEXT_INSTRUCTIONS,
    INFERENCE_ID,
    INVESTIGATOR_AGENT_INSTRUCTIONS,
    REPORT_AGENT_INSTRUCTIONS,
    ROLE_AGENT_IDS,
    STRUCTURED_OUTPUT_INSTRUCTIONS,
    TRIAGE_AGENT_INSTRUCTIONS,
    proposal_agent_definition,
)
from faultline_brain.investigator_agent import InvestigatorValidationError
from faultline_brain.triage import TriageValidationError, run_triage

FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "fixtures"

URL = "https://kibana.example.com"
KEY = "test-key"


def hero_inputs():
    fingerprint = Fingerprint.model_validate_json((FIXTURES / "fingerprint_storm.json").read_text())
    candidates = [Experiment.model_validate(item) for item in json.loads((FIXTURES / "experiments.json").read_text())]
    draft = json.loads((FIXTURES / "triage_hero.json").read_text())
    for key in ("schema_version", "incident_id", "created_at"):
        draft.pop(key)
    return fingerprint, candidates, json.dumps(draft)


def _payload(message="{}", **extra):
    return {"status": "completed", "response": {"message": message}, **extra}


def test_request_body_routes_role_and_forwards_schema_without_conversation():
    bodies = []
    client = AgentBuilderClient(
        URL, KEY, role="triage",
        request=lambda body: bodies.append(body) or _payload("{}"),
    )
    messages = [{"role": "user", "content": "hi"}]
    response_format = {"type": "json_schema", "json_schema": {"name": "t", "schema": {}}}

    client.create(model="ignored-model", messages=messages, response_format=response_format)

    assert len(bodies) == 1
    body = bodies[0]
    assert body["agent_id"] == "faultline-triage"
    assert body["inference_id"] == INFERENCE_ID
    assert body["access_control"] == {"access_mode": "private"}
    assert "conversation_id" not in body
    sent = json.loads(body["input"])
    assert sent["messages"] == messages
    assert sent["response_format"] == response_format
    overrides = body["configuration_overrides"]
    assert overrides["instructions"] == TRIAGE_AGENT_INSTRUCTIONS + "\n" + STRUCTURED_OUTPUT_INSTRUCTIONS
    assert overrides["tools"] == []
    assert overrides["skill_ids"] == []
    assert overrides["enable_elastic_capabilities"] is False


def test_investigator_role_uses_its_own_agent_and_instructions():
    bodies = []
    client = AgentBuilderClient(
        "https://kibana.example.com/s/space-1", KEY, role="investigator",
        request=lambda body: bodies.append(body) or _payload("{}"),
    )
    client.create(model="m", messages=[], response_format={})

    assert bodies[0]["agent_id"] == ROLE_AGENT_IDS["investigator"]
    assert bodies[0]["configuration_overrides"]["instructions"].startswith(
        INVESTIGATOR_AGENT_INSTRUCTIONS[:40]
    )


def test_every_call_is_stateless_and_returns_content_only():
    client = AgentBuilderClient(
        URL, KEY, role="triage",
        request=lambda body: _payload("content-a", conversation_id="conv-1"),
    )
    first = client.create(model="m", messages=[], response_format={})
    second = client.create(model="m", messages=[], response_format={})

    assert first.choices[0].message.content == "content-a"
    assert second.choices[0].message.content == "content-a"
    assert first.usage is None


def test_provenance_records_receipt_without_content_or_secrets():
    events = []
    client = AgentBuilderClient(
        URL, KEY, role="triage", provenance_sink=events.append,
        request=lambda body: _payload("hidden content", conversation_id="conv-9"),
    )
    client.create(model="m", messages=[{"role": "user", "content": "prompt text"}], response_format={})

    assert events == [{
        "provider": "agent_builder",
        "role": "triage",
        "agent_id": "faultline-triage",
        "inference_id": INFERENCE_ID,
        "conversation_id": "conv-9",
        "status": "response_received",
    }]
    rendered = json.dumps(events)
    assert "hidden content" not in rendered and "prompt text" not in rendered and KEY not in rendered


@pytest.mark.parametrize("url", [
    "http://kibana.example.com",
    "https://user:pw@kibana.example.com",
    "https://kibana.example.com/?q=1",
    "https://kibana.example.com/other/path",
    "https://kibana.example.com#frag",
    "",
])
def test_rejects_invalid_kibana_url(url):
    with pytest.raises(AgentBuilderError):
        AgentBuilderClient(url, KEY, role="triage", request=lambda b: {})


@pytest.mark.parametrize("key", ["", "  ", "a\nb", "a\rb"])
def test_rejects_bad_api_key(key):
    with pytest.raises(AgentBuilderError):
        AgentBuilderClient(URL, key, role="triage", request=lambda b: {})


@pytest.mark.parametrize("inference_id", ["", "has space", "a/b", "x?y"])
def test_rejects_bad_inference_id(inference_id):
    with pytest.raises(AgentBuilderError):
        AgentBuilderClient(URL, KEY, role="triage", inference_id=inference_id, request=lambda b: {})


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_rejects_bad_timeout(timeout):
    with pytest.raises(AgentBuilderError):
        AgentBuilderClient(URL, KEY, role="triage", timeout_s=timeout, request=lambda b: {})


def test_rejects_unknown_role():
    with pytest.raises(AgentBuilderError):
        AgentBuilderClient(URL, KEY, role="planner", request=lambda b: {})


@pytest.mark.parametrize("payload", [
    "not-a-dict",
    {"status": "failed", "response": {"message": "{}"}},
    {"status": "completed"},
    {"status": "completed", "response": "nope"},
    {"status": "completed", "response": {"message": ""}},
    {"status": "completed", "response": {"message": "   "}},
    {"status": "completed", "response": {"message": "{}"}, "steps": "bad"},
    {"status": "completed", "response": {"message": "{}"}, "steps": [42]},
    {"status": "completed", "response": {"message": "{}"}, "steps": [{"type": "tool_call"}]},
])
def test_rejects_malformed_or_tool_using_payloads(payload):
    client = AgentBuilderClient(URL, KEY, role="triage", request=lambda body: payload)
    with pytest.raises(AgentBuilderError):
        client.create(model="m", messages=[], response_format={})


def test_transport_failures_are_sanitized():
    def boom(body):
        raise urllib.error.HTTPError(URL, 500, "err", {}, None)

    client = AgentBuilderClient(URL, KEY, role="triage", request=boom)
    with pytest.raises(AgentBuilderError) as exc:
        client.create(model="m", messages=[], response_format={})
    assert URL not in str(exc.value) and KEY not in str(exc.value)


def test_from_env_reads_only_the_builder_key(monkeypatch):
    monkeypatch.setenv("KIBANA_URL", URL)
    monkeypatch.setenv("ELASTIC_AGENT_BUILDER_API_KEY", KEY)
    client = AgentBuilderClient.from_env(role="triage")
    assert client._agent_id == "faultline-triage"


def test_run_triage_accepts_a_valid_builder_response():
    fingerprint, candidates, content = hero_inputs()
    client = AgentBuilderClient(
        URL, KEY, role="triage",
        request=lambda body: _payload(content),
    )

    result = run_triage(client, fingerprint, candidates, "ab-incident")

    assert result.incident_id == "ab-incident"
    assert result.ambiguous is True


def test_run_triage_still_rejects_semantically_invalid_builder_output():
    fingerprint, candidates, content = hero_inputs()
    invalid = json.loads(content)
    invalid["predictions"][0]["during"][0]["metric"] = "db.not_real"
    client = AgentBuilderClient(
        URL, KEY, role="triage",
        request=lambda body: _payload(json.dumps(invalid)),
    )

    with pytest.raises(TriageValidationError, match="unknown metric"):
        run_triage(client, fingerprint, candidates, "ab-incident", max_attempts=1)


def test_report_role_uses_explanation_agent_id_and_report_instructions():
    bodies = []
    client = AgentBuilderClient(
        URL, KEY, role="report",
        request=lambda body: bodies.append(body) or _payload("{}"),
    )
    client.create(model="m", messages=[], response_format={})

    assert bodies[0]["agent_id"] == ROLE_AGENT_IDS["report"]
    assert bodies[0]["configuration_overrides"]["instructions"] == (
        REPORT_AGENT_INSTRUCTIONS + "\n" + STRUCTURED_OUTPUT_INSTRUCTIONS
    )
    assert bodies[0]["configuration_overrides"]["tools"] == []


def test_with_context_binds_snapshot_and_adds_evidence_instructions():
    bodies = []
    base = AgentBuilderClient(
        URL, KEY, role="triage",
        request=lambda body: bodies.append(body) or _payload("{}"),
    )
    context = {"scope": {"incident_id": "inc-1"}, "timeline": {"items": [{"reference": "c1:abc"}]}}
    scoped = base.with_context(context)
    context["scope"]["incident_id"] = "mutated"

    scoped.create(model="m", messages=[], response_format={})
    base.create(model="m", messages=[], response_format={})

    sent = json.loads(bodies[0]["input"])
    assert sent["context"]["scope"]["incident_id"] == "inc-1"
    assert EVIDENCE_CONTEXT_INSTRUCTIONS[:40] in bodies[0]["configuration_overrides"]["instructions"]
    assert "context" not in json.loads(bodies[1]["input"])
    assert EVIDENCE_CONTEXT_INSTRUCTIONS[:40] not in bodies[1]["configuration_overrides"]["instructions"]
    assert base._context is None


def test_with_context_keeps_clients_independent():
    first = AgentBuilderClient(URL, KEY, role="triage", request=lambda b: _payload("{}"))
    second = first.with_context({"a": 1}, provenance_sink=None)
    assert second._context == {"a": 1}
    assert first.with_context({"b": 2})._context == {"b": 2}


@pytest.mark.parametrize("key", [" padded", "padded "])
def test_rejects_padded_api_key(key):
    with pytest.raises(AgentBuilderError):
        AgentBuilderClient(URL, key, role="triage", request=lambda b: {})


@pytest.mark.parametrize("url", [
    "https://kibana.example.com:bad",
    "https://kibana.example.com:99999",
    "https://kibana.example.com/evil\x00",
])
def test_rejects_bad_port_and_control_characters(url):
    with pytest.raises(AgentBuilderError):
        AgentBuilderClient(url, KEY, role="triage", request=lambda b: {})


def test_unsafe_conversation_id_is_dropped_from_provenance():
    events = []
    client = AgentBuilderClient(
        URL, KEY, role="triage", provenance_sink=events.append,
        request=lambda body: _payload("{}", conversation_id="bad id with spaces"),
    )
    client.create(model="m", messages=[], response_format={})
    assert events[0]["conversation_id"] is None


def test_default_transport_posts_bounded_request(monkeypatch):
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            captured["read_limit"] = limit
            return json.dumps(_payload("hello")).encode()

    class _Opener:
        def open(self, request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response()

    handlers = []

    def fake_build_opener(*args):
        handlers.extend(args)
        return _Opener()

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    client = AgentBuilderClient(URL, KEY, role="triage", timeout_s=12)
    result = client.create(model="m", messages=[], response_format={})

    request = captured["request"]
    assert request.get_method() == "POST"
    assert request.full_url == "https://kibana.example.com/api/agent_builder/converse"
    assert request.headers["Authorization"] == f"ApiKey {KEY}"
    assert request.headers["Kbn-xsrf"] == "true"
    assert captured["timeout"] == 12
    assert captured["read_limit"] == 1024 * 1024 + 1
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
    assert any(h is _NoRedirect or isinstance(h, _NoRedirect) for h in handlers)
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil") is None
    assert result.choices[0].message.content == "hello"


def test_deploy_script_dry_run_is_offline_by_default(monkeypatch, capsys):
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "deploy_elastic_investigation_agent.py"
    spec = importlib.util.spec_from_file_location("deploy_agent", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    calls = []
    monkeypatch.setattr(module, "urlopen", lambda *a, **k: calls.append(a) or None)
    for argv in (
        ["deploy", "--role", "triage"],
        ["deploy", "--role", "investigator", "--dry-run"],
        ["deploy"],
    ):
        monkeypatch.setattr("sys.argv", argv)
        module.main()
        out = capsys.readouterr().out
        definition = json.loads(out)["agent"]
        assert calls == []
        if "triage" in argv or "investigator" in argv:
            assert definition["configuration"]["tools"] == []
            assert definition["configuration"]["enable_elastic_capabilities"] is False
        else:
            assert definition["id"] == "faultline-investigation"


def test_proposal_agent_definitions_are_tool_free():
    for role, instructions in (
        ("triage", TRIAGE_AGENT_INSTRUCTIONS),
        ("investigator", INVESTIGATOR_AGENT_INSTRUCTIONS),
    ):
        definition = proposal_agent_definition(role)
        assert definition["id"] == ROLE_AGENT_IDS[role]
        assert definition["configuration"]["instructions"] == instructions
        assert definition["configuration"]["tools"] == []
        assert definition["configuration"]["enable_elastic_capabilities"] is False


class _StubAgent:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def propose(self, *args, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def test_fallback_investigator_returns_primary_proposal():
    primary = _StubAgent(result=SimpleNamespace(ok=True))
    fallback = _StubAgent(result=SimpleNamespace(ok=False))
    events = []
    agent = FallbackInvestigatorAgent(primary, fallback, provenance_sink=events.append)

    assert agent.propose("h").ok is True
    assert fallback.calls == 0
    assert events == [{"provider": "agent_builder", "role": "investigator", "status": "validated"}]


def test_fallback_investigator_falls_back_after_rejection():
    primary = _StubAgent(error=InvestigatorValidationError("bad proposal"))
    fallback = _StubAgent(result="direct-proposal")
    events = []
    agent = FallbackInvestigatorAgent(primary, fallback, provenance_sink=events.append)

    assert agent.propose("h") == "direct-proposal"
    assert [e["status"] for e in events] == ["rejected", "validated"]
    assert events[0]["provider"] == "agent_builder" and events[0]["reason"] == "InvestigatorValidationError"
    assert events[1]["provider"] == "openai"


def test_fallback_investigator_reraises_without_fallback():
    primary = _StubAgent(error=AgentBuilderError("down"))
    agent = FallbackInvestigatorAgent(primary)

    with pytest.raises(AgentBuilderError):
        agent.propose("h")


def test_fallback_investigator_propagates_fallback_failure():
    primary = _StubAgent(error=InvestigatorValidationError("bad"))
    fallback = _StubAgent(error=InvestigatorValidationError("also bad"))
    events = []
    agent = FallbackInvestigatorAgent(primary, fallback, provenance_sink=events.append)

    with pytest.raises(RuntimeError, match="InvestigatorValidationError"):
        agent.propose("h")
    assert [e["status"] for e in events] == ["rejected", "rejected"]
    assert events[1]["provider"] == "openai" and events[1]["reason"] == "InvestigatorValidationError"


def test_fallback_investigator_sanitizes_nonstandard_provider_errors():
    class RemoteError(Exception):
        pass

    primary = _StubAgent(error=InvestigatorValidationError("bad"))
    fallback = _StubAgent(error=RemoteError("secret remote body"))
    events = []
    agent = FallbackInvestigatorAgent(primary, fallback, provenance_sink=events.append)

    with pytest.raises(RuntimeError, match="RemoteError"):
        agent.propose("h")
    rendered = json.dumps(events)
    assert "secret remote body" not in rendered
    assert events[-1] == {"provider": "openai", "role": "investigator", "status": "rejected", "reason": "RemoteError"}
