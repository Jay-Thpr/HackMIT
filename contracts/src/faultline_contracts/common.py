"""Shared conventions for every contract.

Units: *_ms milliseconds, *_qps per second, *_ratio / *_rate in [0, 1].
Timestamps: timezone-aware UTC. A metric with no data is omitted (None), never 0.
"""

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "1"
WINDOW_S = 5  # fixed telemetry window length, seconds


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Model(BaseModel):
    """Base for all contract models: unknown fields are rejected so nothing leaks in silently."""

    model_config = ConfigDict(extra="forbid")
