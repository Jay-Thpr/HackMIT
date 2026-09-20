from datetime import datetime, timedelta
import math

from faultline_contracts import AuditEvent, Fingerprint
from faultline_contracts.audit import AUDIT_INDEX

from .analytics import fingerprint_similarity
from .store import FINGERPRINT_INDEX, production_filter

WINDOW_LIMIT = 120
EVENT_LIMIT = 100
HISTORY_LIMIT = 200


def _utc(value: datetime) -> str:
    if value.utcoffset() != timedelta(0):
        raise ValueError("evidence timestamps must be timezone-aware UTC")
    return value.isoformat()


def _origin(clone_id: str | None) -> dict:
    if clone_id is None:
        return production_filter()
    if not clone_id.strip():
        raise ValueError("clone_id must be nonempty")
    return {"bool": {"filter": [
        {"term": {"environment": "clone"}}, {"term": {"clone_id": clone_id}},
    ]}}


def _matches_origin(document: dict, clone_id: str | None) -> bool:
    if clone_id is None:
        return document.get("clone_id") is None and document.get("environment") in (None, "production")
    return document.get("clone_id") == clone_id and document.get("environment") == "clone"


def _reference(hit: dict, prefix: str) -> str:
    identity = hit.get("_id")
    if not isinstance(identity, str) or not identity or len(identity) > 512:
        raise ValueError("missing evidence identity")
    return f"{prefix}:{identity}"


def _fingerprint(document: dict) -> Fingerprint:
    return Fingerprint.model_validate({k: v for k, v in document.items() if k in Fingerprint.model_fields})


def _window(hit: dict, fingerprint: Fingerprint) -> dict:
    if not all(math.isfinite(v) for v in fingerprint.metrics().values()):
        raise ValueError("nonfinite evidence metrics")
    if not all(math.isfinite(s.threshold) and math.isfinite(s.value) for s in fingerprint.slos):
        raise ValueError("nonfinite SLO evidence")
    return {
        "reference": _reference(hit, "c1"),
        "window_start": fingerprint.window_start.isoformat(),
        "window_end": fingerprint.window_end.isoformat(),
        "metrics": {k: v for k, v in fingerprint.metrics().items() if math.isfinite(v)},
        "slos": [s.model_dump(mode="json") for s in fingerprint.slos],
    }


def _section(items: list, truncated: bool, rejected: int) -> dict:
    return {
        "status": "partial" if truncated or rejected else ("ok" if items else "empty"),
        "items": items, "truncated": truncated, "rejected": rejected,
    }


def _unavailable(exc: Exception) -> dict:
    return {"status": "unavailable", "items": [], "truncated": False, "reason": type(exc).__name__}


class ElasticsearchEvidenceReader:
    def __init__(self, client):
        self._client = getattr(client, "primary", client)

    def _hits(self, index: str, filters: list, sort: list, limit: int, *, exclude: list | None = None) -> list:
        query = {"bool": {"filter": filters}}
        if exclude:
            query["bool"]["must_not"] = exclude
        result = self._client.search(index=index, query=query, sort=sort, size=limit + 1)
        if result.get("timed_out") or result.get("_shards", {}).get("failed", 0):
            raise RuntimeError("incomplete evidence search")
        hits = result["hits"]["hits"]
        if not isinstance(hits, list):
            raise ValueError("invalid evidence search response")
        return hits

    def context(self, incident_id: str, start: datetime, end: datetime, *, clone_id: str | None = None) -> dict:
        if not incident_id.strip() or not timedelta(0) < end - start <= timedelta(hours=6):
            raise ValueError("evidence requires an incident and a bounded positive window")
        scope = {
            "incident_id": incident_id, "environment": "clone" if clone_id else "production",
            "clone_id": clone_id, "start": _utc(start), "end": _utc(end),
        }
        origin = _origin(clone_id)
        common = [{"term": {"incident_id": incident_id}}, origin]
        try:
            hits = self._hits(FINGERPRINT_INDEX, [*common,
                {"range": {"window_start": {"gte": scope["start"]}}},
                {"range": {"window_end": {"lte": scope["end"]}}},
            ], [{"window_start": "desc"}], WINDOW_LIMIT)
            windows, rejected = [], 0
            for hit in hits[:WINDOW_LIMIT]:
                try:
                    doc = hit["_source"]
                    fp = _fingerprint(doc)
                    if doc.get("incident_id") != incident_id or not _matches_origin(doc, clone_id):
                        raise ValueError("evidence origin mismatch")
                    if not start <= fp.window_start < fp.window_end <= end:
                        raise ValueError("evidence window mismatch")
                    windows.append(_window(hit, fp))
                except (ValueError, TypeError, KeyError):
                    rejected += 1
            windows.sort(key=lambda item: item["window_start"])
            timeline = _section(windows, len(hits) > WINDOW_LIMIT, rejected)
            latest = max((datetime.fromisoformat(w["window_end"]) for w in windows), default=None)
            timeline["lag_s"] = (end - latest).total_seconds() if latest is not None else None
        except Exception as exc:
            timeline = _unavailable(exc)
        try:
            hits = self._hits(AUDIT_INDEX, [*common,
                {"range": {"ts": {"gte": scope["start"], "lt": scope["end"]}}},
            ], [{"ts": "desc"}, {"event_id": "desc"}], EVENT_LIMIT)
            events, rejected = [], 0
            keys = ("event_id", "incident_id", "ts", "stage", "kind", "actor", "action_id", "experiment_id")
            for hit in hits[:EVENT_LIMIT]:
                try:
                    doc = hit["_source"]
                    if not doc.get("event_id") or "ts" not in doc:
                        raise ValueError("missing audit evidence identity or timestamp")
                    event = AuditEvent.model_validate({**{k: doc[k] for k in keys if k in doc}, "summary": ""})
                    if event.incident_id != incident_id or not _matches_origin(doc, clone_id):
                        raise ValueError("evidence origin mismatch")
                    if not start <= event.ts < end:
                        raise ValueError("evidence event time mismatch")
                    events.append({"reference": _reference(hit, "c4"), **{
                        k: event.model_dump(mode="json")[k] for k in keys
                    }})
                except (ValueError, TypeError, KeyError):
                    rejected += 1
            events.sort(key=lambda item: (item["ts"], item["event_id"]))
            audit = _section(events, len(hits) > EVENT_LIMIT, rejected)
        except Exception as exc:
            audit = _unavailable(exc)
        statuses = {timeline["status"], audit["status"]}
        status = "unavailable" if statuses == {"unavailable"} else (
            "partial" if statuses & {"unavailable", "partial"} else ("empty" if statuses == {"empty"} else "ok")
        )
        return {"source": "primary_elasticsearch", "scope": scope, "status": status,
                "timeline": timeline, "audit": audit}

    def similar_incidents(self, fingerprint: Fingerprint, *, incident_id: str, before: datetime) -> dict:
        if not incident_id.strip():
            raise ValueError("incident_id is required")
        _utc(before)
        if before > fingerprint.window_start:
            raise ValueError("history must precede the current observation")
        start = before - timedelta(days=1)
        scope = {"start": _utc(start), "end": _utc(before), "excluded_incident_id": incident_id,
                 "environment": "production", "candidate_limit": HISTORY_LIMIT}
        try:
            hits = self._hits(FINGERPRINT_INDEX, [production_filter(),
                {"exists": {"field": "incident_id"}},
                {"range": {"window_start": {"gte": scope["start"]}}},
                {"range": {"window_end": {"lte": scope["end"]}}},
                {"term": {"slos.breached": True}},
            ], [{"window_start": "desc"}], HISTORY_LIMIT,
                exclude=[{"term": {"incident_id": incident_id}}])
            best, rejected = {}, 0
            for hit in hits[:HISTORY_LIMIT]:
                try:
                    doc = hit["_source"]
                    previous_id = doc.get("incident_id")
                    fp = _fingerprint(doc)
                    if not isinstance(previous_id, str) or not previous_id or previous_id == incident_id:
                        raise ValueError("history incident mismatch")
                    if not _matches_origin(doc, None) or not start <= fp.window_start < fp.window_end <= before:
                        raise ValueError("history scope mismatch")
                    if not any(s.breached for s in fp.slos) or not all(math.isfinite(v) for v in fp.metrics().values()):
                        raise ValueError("history has no usable breached observation")
                    score, shared = fingerprint_similarity(fingerprint, fp)
                    if not shared or not math.isfinite(score):
                        raise ValueError("history has no shared measured metrics")
                    row = {**_window(hit, fp), "incident_id": previous_id, "score": score, "compared_metrics": shared}
                    if previous_id not in best or (score, row["reference"]) > (best[previous_id]["score"], best[previous_id]["reference"]):
                        best[previous_id] = row
                except (ValueError, TypeError, KeyError):
                    rejected += 1
            ranked = sorted(best.values(), key=lambda row: (-row["score"], row["incident_id"]))[:5]
            result = _section(ranked, len(hits) > HISTORY_LIMIT, rejected)
        except Exception as exc:
            result = _unavailable(exc)
        return {**result, "scope": scope, "score_kind": "relative_metric_similarity_not_diagnosis"}
