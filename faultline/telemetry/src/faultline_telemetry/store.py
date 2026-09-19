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

    def write(
        self,
        fingerprint: Fingerprint,
        *,
        incident_id: str | None = None,
        clone_id: str | None = None,
    ) -> None:
        """Persist a C1 value with searchable, non-C1 origin metadata.

        ``Fingerprint`` deliberately has no environment or clone fields: it is
        a portable observation contract.  Keeping those identifiers alongside
        (rather than inside) the C1 payload lets Elasticsearch distinguish a
        clone from production without making that metadata visible to C1
        consumers or passive ambiguity checks.
        """
        document = fingerprint.model_dump(mode="json")
        document["environment"] = "clone" if clone_id is not None else "production"
        if incident_id is not None:
            document["incident_id"] = incident_id
        if clone_id is not None:
            document["clone_id"] = clone_id
        self._client.index(index=self._index, document=document)

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

    def query(
        self,
        start: datetime,
        end: datetime,
        *,
        incident_id: str | None = None,
        clone_id: str | None = None,
    ) -> list[Fingerprint]:
        """Read C1 observations for an incident or clone without changing C1."""
        return self._search(start, end, incident_id=incident_id, clone_id=clone_id)

    def _search(
        self,
        start: datetime,
        end: datetime,
        *,
        incident_id: str | None = None,
        clone_id: str | None = None,
    ) -> list[Fingerprint]:
        filters: list[dict[str, Any]] = [
            {"range": {"window_start": {"gte": start.isoformat(), "lt": end.isoformat()}}}
        ]
        if incident_id is not None:
            filters.append({"term": {"incident_id": incident_id}})
        if clone_id is not None:
            filters.append({"term": {"clone_id": clone_id}})
        response = self._client.search(
            index=self._index,
            query={"bool": {"filter": filters}},
            sort=[{"window_start": "asc"}],
        )
        return [
            Fingerprint.model_validate(
                {name: value for name, value in hit["_source"].items() if name in Fingerprint.model_fields}
            )
            for hit in response.get("hits", {}).get("hits", [])
        ]
