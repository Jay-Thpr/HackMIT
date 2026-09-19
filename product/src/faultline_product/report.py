from faultline_contracts import AuditEvent, AuditSink, EventKind


def render_report(audit: AuditSink, incident_id: str) -> str:
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
    lines.extend(f"  {event.ts.isoformat()}  {event.kind.value:<18} {event.summary}" for event in events)
    return "\n".join(lines)


def _payload_value(events: list[AuditEvent], kind: EventKind, key: str, fallback: str) -> str:
    for event in events:
        if event.kind == kind and key in event.payload:
            return str(event.payload[key])
    return fallback
