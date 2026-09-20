import json
from pathlib import Path

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


@pytest.mark.parametrize("name", ["fingerprint_storm.json", "fingerprint_degraded_db.json"])
def test_fixture_evidence_has_no_hidden_world_label_or_fault_timing(name):
    fingerprint = Fingerprint.model_validate_json((FIXTURES / name).read_text())
    evidence = fixture_evidence(fingerprint)
    rendered = json.dumps(evidence).lower()

    assert "window_start" not in evidence
    assert "window_end" not in evidence
    assert all(marker not in rendered for marker in ("storm", "degraded", "fault", "world", "cpu_starve"))
    assert "db.query_p50_ms" in evidence["metrics"]
