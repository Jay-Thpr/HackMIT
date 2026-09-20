"""Operator-facing incident memory backed by Elastic's managed Jina endpoint.

This module is intentionally isolated from C1, C2, and the orchestrator's
verdict path.  It stores curated, human-readable reports and lets an operator
recall similarly worded past reports.  Retrieval is context only; it cannot
classify an incident, select a lever, or change a mathematical verdict.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .indices import INCIDENT_MEMORY_INDEX


class _MemoryClient(Protocol):
    def index(self, *, index: str, document: dict[str, Any]) -> Any: ...

    def search(
        self, *, index: str, query: dict[str, Any], sort: list[dict[str, str]], size: int = 10000
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class IncidentMemory:
    """A curated report safe to retrieve for a human investigation.

    ``content`` must be a report written from already-visible, recorded
    evidence.  It is deliberately not populated from raw telemetry, hidden
    fault-controller state, or an unreviewed model completion.
    """

    incident_id: str
    content: str
    diagnosis: str | None = None
    environment: str = "production"
    clone_id: str | None = None
    created_at: datetime | None = None

    def document(self) -> dict[str, Any]:
        if not self.incident_id.strip():
            raise ValueError("incident_id is required")
        if not self.content.strip():
            raise ValueError("content is required")
        if self.environment not in {"production", "clone"}:
            raise ValueError("environment must be production or clone")
        if self.environment == "clone" and not self.clone_id:
            raise ValueError("clone memory requires clone_id")
        if self.environment == "production" and self.clone_id:
            raise ValueError("production memory cannot have clone_id")

        created_at = self.created_at or datetime.now(timezone.utc)
        if created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        document: dict[str, Any] = {
            "incident_id": self.incident_id,
            "created_at": created_at.astimezone(timezone.utc).isoformat(),
            "environment": self.environment,
            "content": self.content,
        }
        if self.clone_id:
            document["clone_id"] = self.clone_id
        if self.diagnosis:
            document["diagnosis"] = self.diagnosis
        return document


@dataclass(frozen=True)
class IncidentMemoryMatch:
    incident_id: str
    content: str
    diagnosis: str | None
    score: float | None
    created_at: str | None


class ElasticsearchIncidentMemory:
    """Write and semantically retrieve curated incident reports.

    A ``match`` query against Elasticsearch's ``semantic_text`` field invokes
    the configured managed Jina embedding endpoint.  The environment filter is
    a Query DSL pre-filter, so clone reports never appear in production recall.
    """

    def __init__(self, client: _MemoryClient):
        self._client = client

    def write(self, memory: IncidentMemory) -> None:
        self._client.index(index=INCIDENT_MEMORY_INDEX, document=memory.document())

    def search(self, text: str, *, environment: str = "production", limit: int = 5) -> list[IncidentMemoryMatch]:
        if not text.strip():
            raise ValueError("search text is required")
        if environment not in {"production", "clone"}:
            raise ValueError("environment must be production or clone")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        response = self._client.search(
            index=INCIDENT_MEMORY_INDEX,
            query={
                "bool": {
                    "must": [{"match": {"content": {"query": text}}}],
                    "filter": [{"term": {"environment": {"value": environment}}}],
                }
            },
            sort=[{"_score": "desc"}, {"created_at": "desc"}],
            size=limit,
        )
        matches: list[IncidentMemoryMatch] = []
        for hit in response.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            incident_id, content = source.get("incident_id"), source.get("content")
            if not isinstance(incident_id, str) or not isinstance(content, str):
                continue
            score = hit.get("_score")
            matches.append(
                IncidentMemoryMatch(
                    incident_id=incident_id,
                    content=content,
                    diagnosis=source.get("diagnosis") if isinstance(source.get("diagnosis"), str) else None,
                    score=float(score) if isinstance(score, int | float) else None,
                    created_at=source.get("created_at") if isinstance(source.get("created_at"), str) else None,
                )
            )
        return matches
