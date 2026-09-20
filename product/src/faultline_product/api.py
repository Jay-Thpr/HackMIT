"""Read-only API that feeds the UI from what the pipeline actually recorded.

  GET /api/incidents                         -> [{id, started_at, events, diagnosis}]
  GET /api/incidents/{id}/events             -> C4 audit events (JSON)
  GET /api/incidents/{id}/scenario           -> UI Scenario (see ui_scenario.py)
  GET /api/incidents/{id}/series[?clone_id=] -> C1 windows from Elasticsearch (when configured)
  GET /api/health
  /                                          -> the built UI (product/ui/dist), if present

Sources: the C4 audit JSONL (`--audit-log`, default product/state/faultline-audit.jsonl; several
files may be given) and, when FAULTLINE_ELASTICSEARCH_URL is set, Owner 2's fingerprint store for
node readings. Nothing here can act on the system: no levers, no lab, no fault controller.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from faultline_contracts import AuditEvent, EventKind, Fingerprint
from faultline_telemetry import ElasticsearchFingerprintStore, HttpElasticsearchClient

from .paths import PRODUCT_ROOT
from .ui_scenario import scenario_from_incident

UI_DIST = PRODUCT_ROOT / "ui" / "dist"


class IncidentReader:
    def __init__(self, audit_paths: list[Path], store: ElasticsearchFingerprintStore | None):
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


def build_store() -> ElasticsearchFingerprintStore | None:
    url = os.environ.get("FAULTLINE_ELASTICSEARCH_URL")
    if not url:
        return None
    return ElasticsearchFingerprintStore(
        HttpElasticsearchClient(url, api_key=os.environ.get("FAULTLINE_ELASTICSEARCH_API_KEY"))
    )


def create_app(audit_paths: list[Path], store: ElasticsearchFingerprintStore | None = None) -> FastAPI:
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
        production = reader.windows(incident_id, evs)
        clones = {cid: reader.windows(incident_id, evs, cid) for cid in reader.clone_ids(evs)}
        return scenario_from_incident(incident_id, evs, production, clones)

    if UI_DIST.exists():
        app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def ui(path: str):
            candidate = UI_DIST / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(UI_DIST / "index.html")

    return app
