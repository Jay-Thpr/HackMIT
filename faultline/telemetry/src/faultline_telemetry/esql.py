"""ES|QL incident timeline: a readable per-window metric summary for the CLI."""

from datetime import datetime
from typing import Any, Protocol

from .store import FINGERPRINT_INDEX

TIMELINE_QUERY = (
    "FROM {index} "
    '| WHERE incident_id == ?incident AND environment == "production" '
    "AND window_start >= TO_DATETIME(?start) AND window_start < TO_DATETIME(?end) "
    "| KEEP window_start, services.orders.qps, services.orders.retry_ratio, "
    "db.query_p99_ms, slos.value "
    "| SORT window_start"
)


class _EsqlClient(Protocol):
    def esql(self, query: str, params: list[dict[str, Any]] | None = None) -> dict[str, Any]: ...


def incident_timeline(
    client: _EsqlClient,
    incident_id: str,
    start: datetime,
    end: datetime,
    *,
    index: str = FINGERPRINT_INDEX,
) -> list[dict[str, Any]]:
    """Return one row per production C1 window: orders qps/retry ratio, db p99, SLO."""
    response = client.esql(
        TIMELINE_QUERY.format(index=index),
        params=[
            {"incident": incident_id},
            {"start": start.isoformat()},
            {"end": end.isoformat()},
        ],
    )
    columns = [column["name"] for column in response.get("columns", [])]
    return [dict(zip(columns, row)) for row in response.get("values", [])]
