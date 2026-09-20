"""Small HTTP implementation of the Track 2 Elasticsearch port."""

import logging
from collections.abc import Callable
from typing import Any

import httpx


class HttpElasticsearchClient:
    """Use Elasticsearch's document and search APIs through the narrow port."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        name: str | None = None,
    ):
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=10.0)
        if api_key:
            self._client.headers["Authorization"] = f"ApiKey {api_key}"
        self.name = name

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


class MirroredElasticsearchClient:
    """Dual-write an authoritative primary Elasticsearch to best-effort mirrors.

    Writes (index, put_index_template, refresh) go to the primary first and its
    result/errors propagate unchanged; each mirror is then attempted best-effort
    and its failures are reported via ``on_error`` (or logged) but never raised.
    Reads (search, esql) hit only the primary — mirrors are display copies.
    """

    def __init__(
        self,
        primary: HttpElasticsearchClient,
        mirrors: list[HttpElasticsearchClient],
        *,
        on_error: Callable[[str, Exception], None] | None = None,
    ):
        self.primary = primary
        self.mirrors = mirrors
        self._on_error = on_error

    def _mirror_names(self) -> list[str]:
        return [getattr(m, "name", None) or f"mirror-{i}" for i, m in enumerate(self.mirrors)]

    def _mirror_write(self, method: str, **kwargs: Any) -> None:
        for mirror, name in zip(self.mirrors, self._mirror_names()):
            try:
                getattr(mirror, method)(**kwargs)
            except Exception as exc:
                if self._on_error is not None:
                    self._on_error(name, exc)
                else:
                    logging.getLogger(__name__).warning("mirror %s %s failed: %s", name, method, exc)

    def index(self, *, index: str, document: dict[str, Any]) -> Any:
        result = self.primary.index(index=index, document=document)
        self._mirror_write("index", index=index, document=document)
        return result

    def search(
        self,
        *,
        index: str,
        query: dict[str, Any],
        sort: list[dict[str, str]],
        size: int = 10000,
    ) -> dict[str, Any]:
        return self.primary.search(index=index, query=query, sort=sort, size=size)

    def put_index_template(self, name: str, body: dict[str, Any]) -> Any:
        result = self.primary.put_index_template(name, body)
        self._mirror_write("put_index_template", name=name, body=body)
        return result

    def refresh(self, index: str) -> Any:
        result = self.primary.refresh(index)
        self._mirror_write("refresh", index=index)
        return result

    def esql(self, query: str, params: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return self.primary.esql(query, params)
