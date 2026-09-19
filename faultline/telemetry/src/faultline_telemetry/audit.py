"""C4 audit sink backed by Elasticsearch."""

from typing import Any

from faultline_contracts.audit import AUDIT_INDEX, AuditEvent, AuditSink, ExperimentWindow, experiment_windows

from .ports import ElasticsearchPort


class ElasticsearchAuditSink(AuditSink):
    """Write/query audit events without changing their C4 contract shape."""

    def __init__(self, client: ElasticsearchPort, index: str = AUDIT_INDEX):
        self._client = client
        self._index = index

    def write(self, event: AuditEvent, *, clone_id: str | None = None) -> None:
        """Write C4 with optional clone identity kept outside the C4 payload."""
        document = event.model_dump(mode="json")
        document["environment"] = "clone" if clone_id is not None else "production"
        if clone_id is not None:
            document["clone_id"] = clone_id
        self._client.index(index=self._index, document=document)

    def query(self, incident_id: str, *, clone_id: str | None = None) -> list[AuditEvent]:
        filters: list[dict[str, Any]] = [{"term": {"incident_id.keyword": incident_id}}]
        if clone_id is not None:
            filters.append({"term": {"clone_id.keyword": clone_id}})
        response = self._client.search(
            index=self._index,
            query={"bool": {"filter": filters}},
            sort=[{"ts": "asc"}, {"event_id": "asc"}],
        )
        hits: list[dict[str, Any]] = response.get("hits", {}).get("hits", [])
        return [
            AuditEvent.model_validate(
                {name: value for name, value in hit["_source"].items() if name in AuditEvent.model_fields}
            )
            for hit in hits
        ]

    def experiment_history(self, incident_id: str, *, clone_id: str | None = None) -> list[ExperimentWindow]:
        """Return C4-derived experiment boundaries for a production incident or clone."""
        return experiment_windows(self.query(incident_id, clone_id=clone_id))
