"""Elasticsearch-backed persistence and C1 reads for telemetry fingerprints."""

from datetime import datetime
from typing import Any

from faultline_contracts.common import WINDOW_S
from faultline_contracts.fingerprint import Fingerprint

from .ports import ElasticsearchPort

FINGERPRINT_INDEX = "faultline-fingerprints"


class ElasticsearchFingerprintStore:
    """Persist C1 windows and retrieve them as a TelemetrySource."""

    def __init__(self, client: ElasticsearchPort, index: str = FINGERPRINT_INDEX):
        self._client, self._index = client, index

    def write(self, fingerprint: Fingerprint) -> None:
        self._client.index(index=self._index, document=fingerprint.model_dump(mode="json"))

    def window(self, start: datetime, end: datetime) -> Fingerprint:
        matches = self._search(start, end)
        exact = [item for item in matches if item.window_start == start and item.window_end == end]
        if len(exact) != 1:
            raise ValueError(f"expected one fingerprint for [{start}, {end}), got {len(exact)}")
        return exact[0]

    def series(self, start: datetime, end: datetime, step_s: int = WINDOW_S) -> list[Fingerprint]:
        if step_s != WINDOW_S:
            raise ValueError(f"C1 requires {WINDOW_S}s windows")
        return self._search(start, end)

    def _search(self, start: datetime, end: datetime) -> list[Fingerprint]:
        response = self._client.search(
            index=self._index,
            query={"range": {"window_start": {"gte": start.isoformat(), "lt": end.isoformat()}}},
            sort=[{"window_start": "asc"}],
        )
        return [Fingerprint.model_validate(hit["_source"]) for hit in response.get("hits", {}).get("hits", [])]
