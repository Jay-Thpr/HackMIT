"""Small HTTP implementation of the Track 2 Elasticsearch port."""

import hashlib
import json
from typing import Any

import httpx


def document_id(index: str, document: dict[str, Any]) -> str:
    origin = {key: document.get(key) for key in ("incident_id", "environment", "clone_id")}
    if "event_id" in document:
        identity = {**origin, "event_id": document["event_id"]}
    elif "window_start" in document and "window_end" in document:
        identity = {**origin, "window_start": document["window_start"], "window_end": document["window_end"]}
    else:
        identity = document
    encoded = json.dumps([index, identity], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class HttpElasticsearchClient:
    """Use Elasticsearch's document and search APIs through the narrow port."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        *,
        name: str = "primary",
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(base_url=self.base_url, timeout=10.0)
        if api_key:
            self._client.headers["Authorization"] = f"ApiKey {api_key}"

    def index(self, *, index: str, document: dict[str, Any]) -> Any:
        response = self._client.put(f"/{index}/_doc/{document_id(index, document)}", json=document)
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

    def close(self) -> None:
        self._client.close()

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


def __getattr__(name: str):
    if name == "MirroredElasticsearchClient":
        from .mirror import MirroredElasticsearchClient

        return MirroredElasticsearchClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
