"""Read-only Elastic Agent Builder configuration for Faultline explanations.

This is deliberately separate from C2.  The Elastic agent can retrieve bounded,
human-readable evidence and explain relationships in it; C2 remains the only
component that proposes hypotheses, plans experiments, or issues a verdict.
"""

from typing import Any

from faultline_contracts.fingerprint import Fingerprint

from .telemetry import assert_no_leak

AGENT_ID = "faultline-investigation"
INFERENCE_ID = "faultline-openai-investigation"

# These are the only custom-tool IDs Owner 2 may attach to this agent.  In
# particular, do not add a generic index-search tool: that would let the model
# discover C5/controller documents or hidden benchmark metadata.
OWNER2_TOOL_IDS = (
    "faultline.incident_timeline",
    "faultline.clone_vs_production",
    "faultline.similar_incidents",
    "faultline.incident_context",
)

SYSTEM_INSTRUCTIONS = """You are Faultline Investigation, a read-only evidence
explainer. You may use only the four assigned Faultline read tools. Explain
observable relationships returned by those tools, cite the metric names and
values you used, and distinguish an observation from an inference. For example:
\"retry ratio remained elevated after DB latency changed\".

Never call or request a lever, experiment, workflow, controller, fault API,
clone action, or any C5/fault-controller data. Never search arbitrary indices.
Never infer, name, or guess hidden world labels, injected-fault timing, or
benchmark-controller state. Never issue a diagnosis, select an experiment,
rank hypotheses, assert confirmation, or state a final verdict. If asked for
any of those, say that Faultline Brain's C2 triage and noise-model judge own
that decision, then provide only the relevant observed evidence.

Do not make up evidence. If a tool returns no result or the evidence is
insufficient, say so plainly. Your response is an explanation for a human;
it is not an input to, substitute for, or bypass of the normal Faultline
reasoning path."""


def openai_inference_definition(api_key: str, model_id: str) -> dict[str, Any]:
    """Return the Elastic Inference API body for the OpenAI chat endpoint."""
    if not api_key:
        raise ValueError("an OpenAI API key is required")
    if not model_id:
        raise ValueError("an OpenAI model id is required")
    return {
        "service": "openai",
        "service_settings": {"api_key": api_key, "model_id": model_id},
    }


def agent_definition() -> dict[str, Any]:
    """Return an Agent Builder definition with a closed, read-only tool surface."""
    return {
        "id": AGENT_ID,
        "name": "Faultline Investigation",
        "description": "Read-only explanation of bounded Faultline incident evidence.",
        "labels": ["faultline", "read-only", "evidence"],
        "avatar_color": "#D3F3EE",
        "avatar_symbol": "🔎",
        "configuration": {
            "instructions": SYSTEM_INSTRUCTIONS,
            "tools": [{"tool_ids": list(OWNER2_TOOL_IDS)}],
        },
    }


def assert_agent_boundary(definition: dict[str, Any]) -> None:
    """Reject a manifest that could broaden the investigation agent's authority."""
    if definition.get("id") != AGENT_ID:
        raise ValueError("unexpected investigation agent id")
    tools = definition.get("configuration", {}).get("tools")
    if tools != [{"tool_ids": list(OWNER2_TOOL_IDS)}]:
        raise ValueError("investigation agent must use exactly the Owner 2 read tools")
    instructions = definition.get("configuration", {}).get("instructions", "")
    required = (
        "Never call or request a lever",
        "Never infer, name, or guess hidden world labels",
        "Never issue a diagnosis",
        "C2 triage and noise-model judge own",
    )
    if not isinstance(instructions, str) or any(text not in instructions for text in required):
        raise ValueError("investigation agent is missing a required safety instruction")


def fixture_evidence(fingerprint: Fingerprint) -> dict[str, Any]:
    """Make a label- and hidden-timing-free fixture envelope for boundary tests.

    This is intentionally not a C2 request. It contains only visible C1 metric,
    SLO, log, and change-event facts, and omits fixture filenames, window times,
    fault state, and benchmark metadata.
    """
    assert_no_leak(fingerprint)
    return {
        "metrics": dict(sorted(fingerprint.metrics().items())),
        "slos": [
            {"name": slo.name, "metric": slo.metric, "value": slo.value, "breached": slo.breached}
            for slo in fingerprint.slos
        ],
        "log_highlights": [
            {"service": log.service, "level": log.level, "message": log.message, "count": log.count}
            for log in fingerprint.log_highlights
        ],
        "change_events": [
            {"kind": change.kind, "target": change.target, "detail": change.detail}
            for change in fingerprint.change_events
        ],
    }
