"""C1 — Fingerprint: one telemetry window, produced by the telemetry adapter.

Consumers read either the structured fields or the flat `metrics()` map whose keys
are the canonical metric keys (see metrics.py) used by predictions and verdicts.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import Field

from .common import SCHEMA_VERSION, WINDOW_S, Model


class ServiceStats(Model):
    qps: float | None = None
    p50_ms: float | None = None
    p99_ms: float | None = None
    error_rate: float | None = None
    retry_ratio: float | None = None  # attempts per logical request (1.0 = no retries)
    timeout_rate: float | None = None


class DbStats(Model):
    qps: float | None = None
    query_p50_ms: float | None = None
    query_p99_ms: float | None = None
    pool_busy_ratio: float | None = None


class Edge(Model):
    src: str
    dst: str
    qps: float | None = None
    p99_ms: float | None = None
    error_rate: float | None = None


class SloStatus(Model):
    name: str  # e.g. "checkout"
    metric: str  # canonical metric key the SLO is evaluated on, e.g. "svc.gateway.p99_ms"
    threshold: float
    value: float | None = None
    breached: bool


class LogHighlight(Model):
    service: str
    level: str
    message: str  # clustered/templated message, not raw lines
    count: int


class ChangeEvent(Model):
    ts: datetime
    kind: str  # "deploy" | "config" | "flag" | ...
    target: str
    detail: str = ""


class Fingerprint(Model):
    schema_version: str = SCHEMA_VERSION
    window_start: datetime
    window_end: datetime
    services: dict[str, ServiceStats] = Field(default_factory=dict)
    db: DbStats | None = None
    edges: list[Edge] = Field(default_factory=list)
    slos: list[SloStatus] = Field(default_factory=list)
    log_highlights: list[LogHighlight] = Field(default_factory=list)
    change_events: list[ChangeEvent] = Field(default_factory=list)

    def metrics(self) -> dict[str, float]:
        """Flatten to canonical metric keys, skipping missing values."""
        out: dict[str, float] = {}

        def put(prefix: str, stats: Model) -> None:
            for field, value in stats.model_dump().items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    out[f"{prefix}.{field}"] = float(value)

        for name, stats in self.services.items():
            put(f"svc.{name}", stats)
        if self.db is not None:
            put("db", self.db)
        for e in self.edges:
            put(f"edge.{e.src}.{e.dst}", e.model_copy(update={"src": None, "dst": None}))
        for s in self.slos:
            if s.value is not None:
                out[f"slo.{s.name}.value"] = float(s.value)
        return out


@runtime_checkable
class TelemetrySource(Protocol):
    """Read side of the telemetry adapter. Windows are WINDOW_S seconds, UTC."""

    def window(self, start: datetime, end: datetime) -> Fingerprint: ...

    def series(self, start: datetime, end: datetime, step_s: int = WINDOW_S) -> list[Fingerprint]: ...
