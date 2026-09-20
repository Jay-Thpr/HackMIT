import math
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from faultline_brain.agent_builder import AgentBuilderError
from faultline_brain.elastic_investigation import INFERENCE_ID
from faultline_contracts import AuditEvent, AuditSink, EventKind
from faultline_contracts.metrics import is_valid_metric_key
from faultline_contracts.openai_schema import strict_response_format

from .adapters.evidence import evidence_references


class EvidenceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str
    references: list[str]


class EvidenceExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observations: list[EvidenceObservation]


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
        observations = _grounded_observations(explanation, context)
    except Exception as exc:
        if isinstance(exc, AgentBuilderError):
            detail = exc.category
            if exc.http_status is not None:
                detail += f" HTTP {exc.http_status}"
            return [f"Agent Builder explanation unavailable ({detail})"]
        return [f"Agent Builder explanation unavailable ({type(exc).__name__})"]
    lines = ["Agent Builder-selected measured evidence (not a verdict):"]
    for observation in observations:
        lines.append(f"  {observation['metric']}:")
        for row in observation["values"]:
            lines.append(
                f"    {row['window_start']} -> {row['window_end']}: {row['value']} "
                f"[incident: {row['incident_id']}; ref: {row['reference']}]"
            )
    return lines


def _grounded_observations(explanation: EvidenceExplanation, context: dict) -> list[dict]:
    records = {}

    def collect(node):
        if isinstance(node, dict):
            reference = node.get("reference")
            if isinstance(reference, str) and isinstance(node.get("metrics"), dict):
                if reference in records and records[reference] != node:
                    raise ValueError("conflicting evidence reference")
                records[reference] = node
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(context)
    if len(explanation.observations) > 10:
        raise ValueError("too many observations")
    observations = []
    for observation in explanation.observations:
        if not is_valid_metric_key(observation.metric):
            raise ValueError("unknown metric key")
        if not 1 <= len(observation.references) <= 4:
            raise ValueError("observation requires one to four references")
        values = []
        for reference in dict.fromkeys(observation.references):
            record = records.get(reference)
            if record is None or observation.metric not in record["metrics"]:
                raise ValueError("metric absent from cited evidence")
            value = record["metrics"][observation.metric]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid cited metric value")
            if observation.metric.endswith((".error_rate", ".timeout_rate", ".pool_busy_ratio")) and value > 1:
                raise ValueError("invalid cited rate")
            start, end = record["window_start"], record["window_end"]
            parsed_start, parsed_end = datetime.fromisoformat(start), datetime.fromisoformat(end)
            if parsed_start.utcoffset() != timedelta(0) or parsed_end.utcoffset() != timedelta(0) or parsed_end <= parsed_start:
                raise ValueError("invalid cited observation window")
            values.append({"reference": reference, "window_start": start, "window_end": end,
                           "value": value,
                           "incident_id": record.get("incident_id") or (context.get("scope") or {}).get("incident_id", "unknown")})
        observations.append({"metric": observation.metric, "values": sorted(values, key=lambda row: row["window_start"])})
    return observations


def _check_explanation(explanation: EvidenceExplanation, context: dict) -> None:
    _grounded_observations(explanation, context)


def _payload_value(events: list[AuditEvent], kind: EventKind, key: str, fallback: str) -> str:
    for event in reversed(events):
        if event.kind == kind and key in event.payload:
            return str(event.payload[key])
    return fallback
