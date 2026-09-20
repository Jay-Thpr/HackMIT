"""Make `page_human` reach a person. The audit event is the source of truth; the pager is a
best-effort side channel (webhook or local command) that must never break the incident loop."""

import json
import logging
import subprocess
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

from faultline_contracts import AuditEvent, EventKind


class Pager(Protocol):
    def page(self, event: AuditEvent) -> None: ...


def _message(event: AuditEvent) -> dict[str, Any]:
    return {
        "incident_id": event.incident_id,
        "ts": event.ts.isoformat(),
        "stage": int(event.stage),
        "summary": event.summary,
        "event_id": event.event_id,
        "action_id": event.action_id,
        "report": f"faultline report --incident {event.incident_id}",
    }


class WebhookPager:
    """POST one JSON body per page (Slack incoming webhooks accept it as `text` + fields)."""

    def __init__(self, url: str, timeout_s: float = 5.0, post: Callable[[str, bytes, float], None] | None = None):
        self._url, self._timeout_s = url, timeout_s
        self._post = post or self._http_post

    @staticmethod
    def _http_post(url: str, body: bytes, timeout_s: float) -> None:
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=timeout_s):
            pass

    def page(self, event: AuditEvent) -> None:
        message = _message(event)
        message["text"] = f"[faultline] {event.incident_id}: {event.summary}"
        self._post(self._url, json.dumps(message).encode(), self._timeout_s)


class CommandPager:
    """Run a local command with the page as JSON on stdin (e.g. a notifier script, `say`, or
    `osascript -e 'display notification ...'`)."""

    def __init__(self, argv: list[str], timeout_s: float = 10.0, run: Callable[..., Any] | None = None):
        self._argv, self._timeout_s, self._run = argv, timeout_s, run

    def page(self, event: AuditEvent) -> None:
        (self._run or subprocess.run)(self._argv, input=json.dumps(_message(event)), text=True, timeout=self._timeout_s, check=True,
                  capture_output=True)


class PagingAuditSink:
    """Audit sink decorator: every `page_human` event is also sent to the pager."""

    def __init__(self, inner, pager: Pager, *, log: logging.Logger):
        self._inner, self._pager, self._log = inner, pager, log
        self.paged: list[str] = []

    def write(self, event: AuditEvent, **kwargs) -> None:
        self._inner.write(event, **kwargs) if kwargs else self._inner.write(event)
        if event.kind != EventKind.page_human:
            return
        try:
            self._pager.page(event)
            self.paged.append(event.event_id)
        except Exception as exc:  # noqa: BLE001 - paging is best-effort; the audit already holds the page
            self._log.warning("pager failed for %s %s (%s)", event.incident_id, event.event_id, type(exc).__name__)

    def query(self, incident_id: str):
        return self._inner.query(incident_id)
