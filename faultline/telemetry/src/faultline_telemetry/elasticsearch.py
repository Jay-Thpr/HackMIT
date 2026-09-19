"""Small HTTP implementation of the Track 2 Elasticsearch port."""

from typing import Any

import httpx


class HttpElasticsearchClient:
    """Use Elasticsearch's document and search APIs through the narrow port."""

    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=5.0)

    def index(self, *, index: str, document: dict[str, Any]) -> Any:
        response = self._client.post(f"/{index}/_doc", json=document)
        response.raise_for_status()
        return response.json()

    def search(self, *, index: str, query: dict[str, Any], sort: list[dict[str, str]]) -> dict[str, Any]:
        response = self._client.post(f"/{index}/_search", json={"query": query, "sort": sort})
        response.raise_for_status()
        return response.json()
