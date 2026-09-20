"""Similar past incidents from Owner 2's Elasticsearch fingerprint analytics (C1 similarity)."""

from faultline_contracts import Fingerprint
from faultline_telemetry import ElasticsearchTelemetryAnalytics


class ElasticSimilarIncidents:
    def __init__(self, analytics: ElasticsearchTelemetryAnalytics):
        self._analytics = analytics

    def find(self, fingerprint: Fingerprint, *, exclude_incident_id: str, limit: int) -> list[tuple[str, float]]:
        matches = self._analytics.similar_incidents(fingerprint, exclude_incident_id=exclude_incident_id, limit=limit * 4)
        best: dict[str, float] = {}
        for match in matches:  # one row per incident, its best-matching window
            best[match.incident_id] = max(best.get(match.incident_id, 0.0), match.score)
        return sorted(best.items(), key=lambda item: -item[1])[:limit]
