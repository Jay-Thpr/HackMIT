import json
import importlib.util
from pathlib import Path
from urllib.error import HTTPError

import pytest
from faultline_contracts.fingerprint import Fingerprint

from faultline_brain.elastic_investigation import (
    AGENT_ID,
    INFERENCE_ID,
    OWNER2_TOOL_IDS,
    SYSTEM_INSTRUCTIONS,
    agent_definition,
    assert_agent_boundary,
    fixture_evidence,
    openai_inference_definition,
)

FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "fixtures"


@pytest.fixture
def deploy_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "deploy_elastic_investigation_agent.py"
    spec = importlib.util.spec_from_file_location("deploy_elastic_investigation_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_has_only_the_five_read_only_owner2_tools():
    definition = agent_definition()

    assert definition["id"] == AGENT_ID
    assert definition["configuration"]["tools"] == [{"tool_ids": list(OWNER2_TOOL_IDS)}]
    assert "faultline.semantic_incident_memory" in OWNER2_TOOL_IDS
    assert "platform.core.search" not in json.dumps(definition)
    assert_agent_boundary(definition)


def test_agent_instructions_cannot_offer_a_verdict_or_control_path():
    for phrase in (
        "Never call or request a lever",
        "C5/fault-controller data",
        "Never infer, name, or guess hidden world labels",
        "Never issue a diagnosis",
        "C2 triage and noise-model judge own",
        "cannot establish the current cause",
    ):
        assert phrase in SYSTEM_INSTRUCTIONS


def test_boundary_validator_rejects_an_extra_or_missing_tool():
    definition = agent_definition()
    definition["configuration"]["tools"] = [{"tool_ids": [*OWNER2_TOOL_IDS, "platform.core.search"]}]

    with pytest.raises(ValueError, match="exactly the Owner 2 read tools"):
        assert_agent_boundary(definition)


def test_openai_endpoint_uses_chat_completion_and_requires_credentials():
    assert INFERENCE_ID == "faultline-openai-investigation"
    assert openai_inference_definition("key", "model") == {
        "service": "openai",
        "service_settings": {"api_key": "key", "model_id": "model"},
    }
    with pytest.raises(ValueError, match="API key"):
        openai_inference_definition("", "model")


def test_deploy_reuses_matching_inference_endpoint(deploy_script, monkeypatch):
    calls = []

    def fake_request(method, url, api_key, body=None):
        calls.append((method, url, api_key, body))
        return {"endpoints": [{
            "inference_id": INFERENCE_ID,
            "task_type": "chat_completion",
            "service": "openai",
            "service_settings": {"model_id": "gpt-4.1"},
        }]}

    monkeypatch.setattr(deploy_script, "request", fake_request)
    endpoint = openai_inference_definition("secret", "gpt-4.1")
    assert deploy_script.ensure_inference_endpoint("https://elastic.example", "key", endpoint) == "reused"
    assert [call[0] for call in calls] == ["GET"]


def test_deploy_creates_missing_inference_endpoint(deploy_script, monkeypatch):
    calls = []

    def fake_request(method, url, api_key, body=None):
        calls.append((method, url, api_key, body))
        if method == "GET":
            raise HTTPError(url, 404, "missing", {}, None)
        return {}

    monkeypatch.setattr(deploy_script, "request", fake_request)
    endpoint = openai_inference_definition("secret", "gpt-4.1")
    assert deploy_script.ensure_inference_endpoint("https://elastic.example", "key", endpoint) == "created"
    assert [call[0] for call in calls] == ["GET", "PUT"]
    assert calls[1][3] == endpoint


def test_deploy_rejects_incompatible_inference_endpoint(deploy_script, monkeypatch):
    monkeypatch.setattr(deploy_script, "request", lambda *args, **kwargs: {"endpoints": [{
        "inference_id": INFERENCE_ID,
        "task_type": "chat_completion",
        "service": "openai",
        "service_settings": {"model_id": "different-model"},
    }]})
    with pytest.raises(SystemExit, match="does not match"):
        deploy_script.ensure_inference_endpoint(
            "https://elastic.example", "key", openai_inference_definition("secret", "gpt-4.1")
        )


@pytest.mark.parametrize("name", ["fingerprint_storm.json", "fingerprint_degraded_db.json"])
def test_fixture_evidence_has_no_hidden_world_label_or_fault_timing(name):
    fingerprint = Fingerprint.model_validate_json((FIXTURES / name).read_text())
    evidence = fixture_evidence(fingerprint)
    rendered = json.dumps(evidence).lower()

    assert "window_start" not in evidence
    assert "window_end" not in evidence
    assert all(marker not in rendered for marker in ("storm", "degraded", "fault", "world", "cpu_starve"))
    assert "db.query_p50_ms" in evidence["metrics"]
