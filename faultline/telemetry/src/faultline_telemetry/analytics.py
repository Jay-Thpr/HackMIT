"""Phase 3 Elasticsearch analytics built from C1 fingerprints and C4 audit events.

These queries operate exclusively on observed C1 values and C4 records.  Clone
identity is document metadata; no hidden world, injected cause, or controller
state is accepted or returned.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from faultline_contracts.fingerprint import Fingerprint

from .ports import ElasticsearchPort
from .store import FINGERPRINT_INDEX, production_filter


@dataclass(frozen=True)
class FingerprintRecord:
    """One C1 observation plus its Elasticsearch document identity."""

    fingerprint: Fingerprint
    incident_id: str | None
    clone_id: str | None

    @property
    def environment(self) -> str:
        return "clone" if self.clone_id is not None else "production"


@dataclass(frozen=True)
class SimilarIncident:
    incident_id: str
    score: float
    fingerprint: FingerprintRecord


@dataclass(frozen=True)
class ReproductionSimilarity:
    clone_id: str
    score: float
    compared_metrics: int


def fingerprint_similarity(left: Fingerprint, right: Fingerprint) -> tuple[float, int]:
    """Return a scale-free 0..1 C1 similarity score and overlap count.

    Each shared metric contributes its relative distance. Missing metrics do not
    masquerade as zeros, so they are excluded rather than interpreted as a
    healthy reading.
    """
    left_metrics, right_metrics = left.metrics(), right.metrics()
    shared = sorted(set(left_metrics) & set(right_metrics))
    if not shared:
        return 0.0, 0
    distances = [
        abs(left_metrics[key] - right_metrics[key]) / max(abs(left_metrics[key]), abs(right_metrics[key]), 1.0)
        for key in shared
    ]
    return max(0.0, 1.0 - sum(distances) / len(distances)), len(shared)


class ElasticsearchTelemetryAnalytics:
    """Search fingerprints for the UI, incident history, and clone reproduction."""

    def __init__(self, client: ElasticsearchPort, index: str = FINGERPRINT_INDEX):
        self._client, self._index = client, index

    def ui_data(
        self, incident_id: str, start: datetime, end: datetime, *, clone_id: str | None = None
    ) -> list[FingerprintRecord]:
        """Chronological chart data for one production incident or clone."""
        return self._records(
            start,
            end,
            incident_id=incident_id,
            clone_id=clone_id,
            environment="clone" if clone_id is not None else "production",
        )

    def similar_incidents(
        self,
        fingerprint: Fingerprint,
        *,
        exclude_incident_id: str | None = None,
        limit: int = 10,
    ) -> list[SimilarIncident]:
        """Rank prior production incident windows by observed C1 similarity."""
        if limit < 1:
            raise ValueError("limit must be positive")
        records = self._records(incident_id_required=True, environment="production")
        matches = [
            SimilarIncident(record.incident_id or "", fingerprint_similarity(fingerprint, record.fingerprint)[0], record)
            for record in records
            if record.environment == "production" and record.incident_id != exclude_incident_id
        ]
        return sorted(matches, key=lambda item: (-item.score, item.incident_id, item.fingerprint.fingerprint.window_start))[:limit]

    def clone_production_similarity(
        self, production: Fingerprint, clone: FingerprintRecord
    ) -> ReproductionSimilarity:
        """Compare one clone observation with a production fingerprint."""
        if clone.clone_id is None:
            raise ValueError("clone comparison requires clone-origin telemetry")
        score, compared_metrics = fingerprint_similarity(production, clone.fingerprint)
        return ReproductionSimilarity(clone.clone_id, score, compared_metrics)

    def _records(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        incident_id: str | None = None,
        clone_id: str | None = None,
        environment: str | None = None,
        incident_id_required: bool = False,
    ) -> list[FingerprintRecord]:
        filters: list[dict[str, Any]] = []
        if start is not None or end is not None:
            if start is None or end is None:
                raise ValueError("start and end must be provided together")
            filters.append({"range": {"window_start": {"gte": start.isoformat(), "lt": end.isoformat()}}})
        if incident_id is not None:
            filters.append({"term": {"incident_id": incident_id}})
        elif incident_id_required:
            filters.append({"exists": {"field": "incident_id"}})
        if clone_id is not None:
            filters.append({"term": {"clone_id": clone_id}})
        if environment == "production":
            filters.append(production_filter())
        elif environment is not None:
            filters.append({"term": {"environment": environment}})
        query: dict[str, Any] = {"bool": {"filter": filters}} if filters else {"match_all": {}}
        response = self._client.search(index=self._index, query=query, sort=[{"window_start": "asc"}])
        return [self._record(hit["_source"]) for hit in response.get("hits", {}).get("hits", [])]

    @staticmethod
    def _record(document: dict[str, Any]) -> FingerprintRecord:
        fingerprint = Fingerprint.model_validate(
            {name: value for name, value in document.items() if name in Fingerprint.model_fields}
        )
        return FingerprintRecord(fingerprint, document.get("incident_id"), document.get("clone_id"))
