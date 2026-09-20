from datetime import timedelta

from pydantic import BaseModel, ConfigDict

from faultline_brain.elastic_investigation import INFERENCE_ID
from faultline_contracts import AuditEvent, AuditSink, EventKind
from faultline_contracts.openai_schema import strict_response_format

from .adapters.evidence import evidence_references


class EvidenceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    references: list[str]


class EvidenceExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observations: list[EvidenceObservation]
    limitations: list[str]


def render_report(
    audit: AuditSink,
    incident_id: str,
    *,
    evidence_reader=None,
    explanation_client=None,
) -> str:
    events = audit.query(incident_id)
    if not events:
        raise ValueError(f"no audit events found for incident {incident_id!r}")

    diagnosis = _payload_value(events, EventKind.verdict, "diagnosis", "unknown")
    patch = _payload_value(events, EventKind.patch_opened, "reference", "not created")
    lines = [
        f"Incident: {incident_id}",
        f"Diagnosis: {diagnosis}",
        f"Patch: {patch}",
        "Timeline:",
    ]
    lines.extend(
        f"  {event.ts.isoformat()}  {event.kind.value:<18} {event.summary}" for event in events
    )
    if evidence_reader is not None:
        context = _load_context(evidence_reader, incident_id, events)
        lines.extend(_evidence_lines(context))
        if explanation_client is not None:
            lines.extend(_explanation_lines(explanation_client, context))
    return "\n".join(lines)


def _load_context(reader, incident_id: str, events: list[AuditEvent]) -> dict:
    end = max(event.ts for event in events) + timedelta(microseconds=1)
    start = max(
        min(event.ts for event in events) - timedelta(seconds=120),
        end - timedelta(hours=6),
    )
    try:
        return reader.context(incident_id, start, end)
    except Exception as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}


def _evidence_lines(context: dict) -> list[str]:
    lines = [f"Elastic evidence: {context.get('status', 'unavailable')}"]
    if "reason" in context:
        lines.append(f"  reason: {context['reason']}")
    scope = context.get("scope") or {}
    if scope:
        lines.append(
            f"  scope: {scope.get('environment', '?')} {scope.get('start', '?')} -> {scope.get('end', '?')}"
        )
    for label, section in (("windows", context.get("timeline")), ("audit events", context.get("audit"))):
        if not isinstance(section, dict):
            continue
        items = section.get("items") or []
        line = f"  {label}: {len(items)} returned (RETURNED count, not a total); status={section.get('status', '?')}"
        if section.get("truncated"):
            line += "; truncated"
        if section.get("rejected"):
            line += f"; rejected={section['rejected']}"
        if label == "windows" and section.get("lag_s") is not None:
            line += f"; lag_s={section['lag_s']:g}"
        lines.append(line)
    similar = context.get("similar_incidents")
    if isinstance(similar, dict):
        lines.append(f"  similar incidents: {len(similar.get('items') or [])} returned; status={similar.get('status', '?')}")
    refs = evidence_references(context)
    if refs:
        lines.append(f"  references: {', '.join(refs)}")
    return lines


def _explanation_lines(client, context: dict) -> list[str]:
    try:
        response = client.with_context(context).chat.completions.create(
            model=INFERENCE_ID,
            messages=[],
            response_format=strict_response_format(EvidenceExplanation, "evidence_explanation"),
        )
        explanation = EvidenceExplanation.model_validate_json(response.choices[0].message.content)
        _check_explanation(explanation, context)
    except Exception as exc:
        return [f"Agent Builder explanation unavailable ({type(exc).__name__})"]
    lines = ["Agent Builder explanation (not a verdict):"]
    lines.extend(
        f"  - {observation.text} [refs: {', '.join(sorted(set(observation.references)))}]"
        for observation in explanation.observations
    )
    if explanation.limitations:
        lines.append("  limitations:")
        lines.extend(f"    - {limitation}" for limitation in explanation.limitations)
    return lines


def _check_explanation(explanation: EvidenceExplanation, context: dict) -> None:
    allowed = set(evidence_references(context))
    if len(explanation.observations) > 10:
        raise ValueError("too many observations")
    if len(explanation.limitations) > 10:
        raise ValueError("too many limitations")
    for observation in explanation.observations:
        if not observation.text.strip() or len(observation.text) > 2000:
            raise ValueError("observation text missing or oversized")
        if not observation.references or any(ref not in allowed for ref in observation.references):
            raise ValueError("observation cites a reference outside the supplied evidence")
    for limitation in explanation.limitations:
        if len(limitation) > 2000:
            raise ValueError("limitation oversized")


def _payload_value(events: list[AuditEvent], kind: EventKind, key: str, fallback: str) -> str:
    for event in reversed(events):
        if event.kind == kind and key in event.payload:
            return str(event.payload[key])
    return fallback
