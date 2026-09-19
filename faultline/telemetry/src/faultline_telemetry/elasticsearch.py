"""Small HTTP implementation of the Track 2 Elasticsearch port."""

from typing import Any

import httpx


class HttpElasticsearchClient:
    """Use Elasticsearch's document and search APIs through the narrow port."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ):
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=10.0)
        if api_key:
            self._client.headers["Authorization"] = f"ApiKey {api_key}"

    def index(self, *, index: str, document: dict[str, Any]) -> Any:
        response = self._client.post(f"/{index}/_doc", json=document)
        response.raise_for_status()
        return response.json()

    def search(
        self,
        *,
        index: str,
        query: dict[str, Any],
        sort: list[dict[str, str]],
        size: int = 10000,
    ) -> dict[str, Any]:
        response = self._client.post(
            f"/{index}/_search", json={"query": query, "sort": sort, "size": size}
        )
        response.raise_for_status()
        return response.json()

    def put_index_template(self, name: str, body: dict[str, Any]) -> Any:
        response = self._client.put(f"/_index_template/{name}", json=body)
        response.raise_for_status()
        return response.json()

    def refresh(self, index: str) -> Any:
        response = self._client.post(f"/{index}/_refresh")
        response.raise_for_status()
        return response.json()

    def esql(self, query: str, params: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        response = self._client.post(
            "/_query",
            json={"query": query, "params": params or []},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()
