"""Read-only API that feeds the UI from what the pipeline actually recorded.

  GET /api/incidents                         -> [{id, started_at, events, diagnosis}]
  GET /api/incidents/{id}/events             -> C4 audit events (JSON)
  GET /api/incidents/{id}/scenario           -> UI Scenario (see ui_scenario.py)
  GET /api/incidents/{id}/series[?clone_id=] -> C1 windows from Elasticsearch (when configured)
  GET /api/incidents/{id}/stream             -> Server-Sent Events: the Scenario re-sent whenever the audit
                                                log grows (tail -f), `event: done` once the report is written
  GET /api/health
  /                                          -> the built UI (product/ui/dist), if present

Sources: the C4 audit JSONL (`--audit-log`, default product/state/faultline-audit.jsonl; several
files may be given) and, when FAULTLINE_ELASTICSEARCH_URL is set, Owner 2's fingerprint store for
node readings. Nothing here can act on the system: no levers, no lab, no fault controller.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from faultline_contracts import AuditEvent, EventKind, Fingerprint
from faultline_telemetry import (
    ElasticsearchFingerprintStore,
    HttpElasticsearchClient,
    JsonlFingerprintStore,
)

from .paths import PRODUCT_ROOT
from .ui_scenario import scenario_from_incident

UI_DIST = PRODUCT_ROOT / "ui" / "dist"


class IncidentReader:
    def __init__(self, audit_paths: list[Path],
                 store: ElasticsearchFingerprintStore | JsonlFingerprintStore | None):
        self._paths = audit_paths
        self._store = store

    def events(self, incident_id: str | None = None) -> list[AuditEvent]:
        out: list[AuditEvent] = []
        for path in self._paths:
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                event = AuditEvent.model_validate_json(line)
                if incident_id is None or event.incident_id == incident_id:
                    out.append(event)
        return sorted(out, key=lambda e: (e.ts, e.stage))

    def incidents(self) -> list[dict[str, Any]]:
        by_id: dict[str, list[AuditEvent]] = {}
        for e in self.events():
            by_id.setdefault(e.incident_id, []).append(e)
        rows = []
        for incident_id, evs in by_id.items():
            verdict = next((e for e in reversed(evs) if e.kind == EventKind.verdict), None)
            report = next((e for e in reversed(evs) if e.kind == EventKind.report), None)
            rows.append({
                "id": incident_id,
                "started_at": evs[0].ts.isoformat(),
                "events": len(evs),
                "diagnosis": verdict.payload.get("diagnosis") if verdict else None,
                "outcome": report.summary if report else ("paged" if any(e.kind == EventKind.page_human for e in evs) else "in progress"),
            })
        return sorted(rows, key=lambda r: r["started_at"], reverse=True)

    def windows(self, incident_id: str, evs: list[AuditEvent], clone_id: str | None = None) -> list[Fingerprint]:
        if self._store is None or not evs:
            return []
        start = evs[0].ts - timedelta(minutes=3)
        end = evs[-1].ts + timedelta(minutes=1)
        try:
            return self._store.query(start, end, incident_id=incident_id, clone_id=clone_id)
        except Exception:  # noqa: BLE001 - readings are optional; the audit alone still renders
            return []

    def clone_ids(self, evs: list[AuditEvent]) -> list[str]:
        return sorted({e.payload["clone_id"] for e in evs if isinstance(e.payload, dict) and e.payload.get("clone_id")})


    def scenario(self, incident_id: str, evs: list[AuditEvent], now: datetime | None = None) -> dict[str, Any]:
        production = self.windows(incident_id, evs)
        clones = {cid: self.windows(incident_id, evs, cid) for cid in self.clone_ids(evs)}
        return scenario_from_incident(incident_id, evs, production, clones, now=now)

    def supporting_telemetry(self, incident_id: str, evs: list[AuditEvent]) -> dict[str, Any]:
        """Small, bounded Observability read for a UI evidence panel.

        This is deliberately display-only: C1 and C4 remain the inputs to Faultline's
        decision path. The query does not retrieve span bodies, SQL text, or log messages.
        """
        configured_url = os.environ.get("FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL")
        configured_key = os.environ.get("FAULTLINE_OBSERVABILITY_ELASTICSEARCH_API_KEY")
        if not configured_url or not configured_key:
            return {"state": "not-configured", "incidentId": incident_id,
                    "detail": "Set the Observability mirror URL and read-only API key to show trace and log summaries."}
        if not evs:
            raise ValueError(f"no audit events for {incident_id!r}")
        start, end = evs[0].ts, evs[-1].ts + timedelta(minutes=1)
        filters = [
            {"range": {"@timestamp": {"gte": start.isoformat(), "lt": end.isoformat()}}},
            {"bool": {"should": [
                {"term": {"resource.attributes.deployment.environment": "production"}},
                {"term": {"resource.attributes.deployment.environment.name": "production"}},
            ], "minimum_should_match": 1}},
        ]
        trace_body = {"size": 0, "query": {"bool": {"filter": filters}}, "aggs": {
            "services": {"terms": {"field": "resource.attributes.service.name", "size": 12}},
            "traces": {"cardinality": {"field": "trace_id"}},
        }}
        headers = {"Authorization": f"ApiKey {configured_key}"}
        try:
            with httpx.Client(base_url=configured_url.rstrip("/"), headers=headers, timeout=5.0) as client:
                traces = client.post("/traces-*/_search", json=trace_body)
                traces.raise_for_status()
                logs = client.post("/logs-*/_count", json={"query": {"bool": {"filter": filters}}})
                logs.raise_for_status()
            trace_data = traces.json()
            total = trace_data.get("hits", {}).get("total", 0)
            trace_count = total.get("value", 0) if isinstance(total, dict) else total
            buckets = trace_data.get("aggregations", {}).get("services", {}).get("buckets", [])
            return {
                "state": "available", "incidentId": incident_id,
                "start": start.isoformat(), "end": end.isoformat(),
                "spans": int(trace_count),
                "traces": int(trace_data.get("aggregations", {}).get("traces", {}).get("value", 0)),
                "logs": int(logs.json().get("count", 0)),
                "services": [{"name": str(row["key"]), "spans": int(row["doc_count"])} for row in buckets],
                "detail": "Counts are scoped to the recorded incident window and production deployment environment.",
            }
        except (httpx.HTTPError, ValueError, TypeError):
            return {"state": "unavailable", "incidentId": incident_id,
                    "detail": "Observability evidence could not be read. Diagnosis and replay continue from C1 and C4."}


async def scenario_updates(
    reader: IncidentReader,
    incident_id: str,
    *,
    poll_s: float = 2.0,
    heartbeat_s: float = 15.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> AsyncIterator[str]:
    """SSE frames: a `scenario` frame each time the incident's audit log has grown, `done` after the
    report. Readings are refreshed from Elasticsearch on every frame, so a browser that opened the
    page mid-incident converges on the same picture as one that watched from the start."""
    seen = -1
    idle = 0.0
    while True:
        evs = reader.events(incident_id)
        if len(evs) != seen and evs:
            seen = len(evs)
            idle = 0.0
            scenario = reader.scenario(incident_id, evs, now=clock())
            yield f"event: scenario\ndata: {json.dumps(scenario)}\n\n"
            if scenario.get("complete"):
                yield "event: done\ndata: {}\n\n"
                return
        elif idle >= heartbeat_s:
            idle = 0.0
            yield ": keep-alive\n\n"
        await sleep(poll_s)
        idle += poll_s


def build_store() -> ElasticsearchFingerprintStore | None:
    url = os.environ.get("FAULTLINE_ELASTICSEARCH_URL")
    if not url:
        return None
    return ElasticsearchFingerprintStore(
        HttpElasticsearchClient(url, api_key=os.environ.get("FAULTLINE_ELASTICSEARCH_API_KEY"))
    )


def create_app(audit_paths: list[Path],
               store: ElasticsearchFingerprintStore | JsonlFingerprintStore | None = None) -> FastAPI:
    reader = IncidentReader(audit_paths, store)
    app = FastAPI(title="Faultline UI API", version="1")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "audit_logs": [str(p) for p in audit_paths if p.exists()], "elasticsearch": store is not None}

    @app.get("/api/incidents")
    def incidents() -> list[dict[str, Any]]:
        return reader.incidents()

    @app.get("/api/incidents/{incident_id}/events")
    def events(incident_id: str) -> list[dict[str, Any]]:
        evs = reader.events(incident_id)
        if not evs:
            raise HTTPException(404, f"no audit events for {incident_id!r}")
        return [e.model_dump(mode="json") for e in evs]

    @app.get("/api/incidents/{incident_id}/series")
    def series(incident_id: str, clone_id: str | None = None) -> list[dict[str, Any]]:
        evs = reader.events(incident_id)
        if not evs:
            raise HTTPException(404, f"no audit events for {incident_id!r}")
        return [fp.model_dump(mode="json") for fp in reader.windows(incident_id, evs, clone_id)]

    @app.get("/api/incidents/{incident_id}/scenario")
    def scenario(incident_id: str) -> dict[str, Any]:
        evs = reader.events(incident_id)
        if not evs:
            raise HTTPException(404, f"no audit events for {incident_id!r}")
        return reader.scenario(incident_id, evs, now=datetime.now(timezone.utc))

    @app.get("/api/incidents/{incident_id}/evidence.json")
    def evidence_json(incident_id: str) -> dict[str, Any]:
        """Portable, read-only C1/C2/C4 evidence bundle for judges and incident review."""
        evs = reader.events(incident_id)
        if not evs:
            raise HTTPException(404, f"no audit events for {incident_id!r}")
        return {"incident_id": incident_id, "scenario": reader.scenario(incident_id, evs, now=datetime.now(timezone.utc)),
                "fingerprints": [fp.model_dump(mode="json") for fp in reader.windows(incident_id, evs)],
                "audit_events": [event.model_dump(mode="json") for event in evs]}

    @app.get("/api/incidents/{incident_id}/evidence.md", response_class=PlainTextResponse)
    def evidence_markdown(incident_id: str) -> str:
        evs = reader.events(incident_id)
        if not evs:
            raise HTTPException(404, f"no audit events for {incident_id!r}")
        scenario = reader.scenario(incident_id, evs, now=datetime.now(timezone.utc))
        report = scenario.get("report", {})
        return "\n".join((f"# Faultline evidence: {incident_id}", "", f"- Outcome: {report.get('outcome', 'in progress')}",
            f"- Diagnosis: {report.get('diagnosis') or 'not confirmed'}", f"- Production actions: {report.get('productionActions', 0)}", "", "## Audited timeline", "",
            *[f"- {event.ts.isoformat()} · stage {event.stage} · {event.kind.value}: {event.summary}" for event in evs], ""))

    @app.get("/api/incidents/{incident_id}/supporting-telemetry")
    def supporting_telemetry(incident_id: str) -> dict[str, Any]:
        evs = reader.events(incident_id)
        if not evs:
            raise HTTPException(404, f"no audit events for {incident_id!r}")
        return reader.supporting_telemetry(incident_id, evs)

    @app.get("/api/incidents/{incident_id}/stream")
    def stream(incident_id: str) -> StreamingResponse:
        # No 404 here: the incident may not have written its first event yet (watch is still waiting
        # for the breach); the stream simply starts delivering once it does.
        return StreamingResponse(
            scenario_updates(reader, incident_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if UI_DIST.exists():
        app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def ui(path: str):
            candidate = UI_DIST / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(UI_DIST / "index.html")

    return app
