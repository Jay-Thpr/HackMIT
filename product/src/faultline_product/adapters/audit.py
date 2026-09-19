"""C4 audit fan-out: the local JSONL log stays authoritative, Elasticsearch is best-effort."""

import logging
from typing import Protocol

from faultline_contracts import AuditEvent


class _SecondaryAuditSink(Protocol):
    def write(self, event: AuditEvent, *, clone_id: str | None = None) -> None: ...


class TeeAuditSink:
    """Write audit events to a primary sink and a best-effort secondary.

    The incident loop must never break because Elasticsearch is unreachable, so
    secondary failures are logged and swallowed. Queries read the primary only.
    """

    def __init__(self, primary, secondary: _SecondaryAuditSink, *, log: logging.Logger):
        self._primary = primary
        self._secondary = secondary
        self._log = log

    def write(self, event: AuditEvent, *, clone_id: str | None = None) -> None:
        self._primary.write(event)
        try:
            self._secondary.write(event, clone_id=clone_id)
        except Exception as exc:  # noqa: BLE001 - audit durability must not break the loop
            self._log.warning("secondary audit sink failed for %s: %s", event.event_id, exc)

    def query(self, incident_id: str):
        return self._primary.query(incident_id)
