"""C4 audit sink backed by Elasticsearch."""

from typing import Any

from faultline_contracts.audit import AUDIT_INDEX, AuditEvent, AuditSink

from .ports import ElasticsearchPort


class ElasticsearchAuditSink(AuditSink):
    """Write/query audit events without changing their C4 contract shape."""

    def __init__(self, client: ElasticsearchPort, index: str = AUDIT_INDEX):
        self._client = client
        self._index = index

    def write(self, event: AuditEvent) -> None:
        self._client.index(index=self._index, document=event.model_dump(mode="json"))

    def query(self, incident_id: str) -> list[AuditEvent]:
        response = self._client.search(
            index=self._index,
            query={"term": {"incident_id.keyword": incident_id}},
            sort=[{"ts": "asc"}, {"event_id": "asc"}],
        )
        hits: list[dict[str, Any]] = response.get("hits", {}).get("hits", [])
        return [AuditEvent.model_validate(hit["_source"]) for hit in hits]
