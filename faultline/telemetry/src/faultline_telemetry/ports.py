"""Small, testable external dependency ports for Track 2."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ElasticsearchPort(Protocol):
    """Subset needed for C4 now and C1 query assembly later."""

    def index(self, *, index: str, document: dict[str, Any]) -> Any: ...

    def search(
        self, *, index: str, query: dict[str, Any], sort: list[dict[str, str]], size: int = 10000
    ) -> dict[str, Any]: ...


@runtime_checkable
class StatsSource(Protocol):
    """A snapshot source such as a sandbox `/stats` endpoint or OTel export."""

    def snapshot(self) -> dict[str, Any]: ...
