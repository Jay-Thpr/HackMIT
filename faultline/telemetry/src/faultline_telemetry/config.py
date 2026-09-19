"""Configuration owned by the Track 2 telemetry adapter."""

from dataclasses import dataclass

from faultline_contracts.audit import AUDIT_INDEX
from faultline_contracts.common import WINDOW_S


@dataclass(frozen=True)
class TelemetrySettings:
    """Local sandbox endpoints and fixed contract conventions.

    Envoy admin is intentionally an in-network address only. Host callers must
    never use it, because doing so could bypass target-enforced lever TTLs.
    """

    elasticsearch_url: str = "http://localhost:9200"
    orders_stats_url: str = "http://localhost:8101/stats"
    payments_stats_url: str = "http://localhost:8102/stats"
    loadgen_stats_url: str = "http://localhost:8103/stats"
    envoy_stats_url: str = "http://envoy:9902/stats/prometheus"
    audit_index: str = AUDIT_INDEX
    window_s: int = WINDOW_S

    def __post_init__(self) -> None:
        if self.window_s != WINDOW_S:
            raise ValueError(f"C1 requires fixed {WINDOW_S}s windows")
        if not self.audit_index:
            raise ValueError("audit_index must not be empty")
