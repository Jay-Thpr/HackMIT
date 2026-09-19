"""Live sandbox stats polling exposed through the C1 TelemetrySource protocol."""

from datetime import datetime, timedelta
from typing import Any

import httpx

from faultline_contracts.common import WINDOW_S
from faultline_contracts.fingerprint import Fingerprint

from .fingerprint import fingerprint_from_stats


class HttpSandboxStats:
    """Fetch public sandbox `/stats` endpoints; no internal endpoints are used."""

    def __init__(self, urls: dict[str, str], client: httpx.Client | None = None):
        self._urls = dict(urls)
        self._client = client or httpx.Client(timeout=5.0)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {name: self._client.get(url).raise_for_status().json() for name, url in self._urls.items()}


class PollingTelemetrySource:
    """C1 telemetry source backed by consecutive public stats snapshots.

    Call `poll(start, end)` once per fixed five-second interval. It records a
    fingerprint that downstream consumers can retrieve with the C1 protocol.
    """

    def __init__(self, stats: Any):
        self._stats = stats
        self._previous: dict[str, dict[str, Any]] | None = None
        self._history: list[Fingerprint] = []

    def poll(self, start: datetime, end: datetime) -> Fingerprint | None:
        if end - start != timedelta(seconds=WINDOW_S):
            raise ValueError(f"C1 polling requires exactly {WINDOW_S}s windows")
        current = self._stats.snapshot()
        if self._previous is None:
            self._previous = current
            return None
        fp = fingerprint_from_stats(self._previous, current, start, end)
        self._previous = current
        self._history.append(fp)
        return fp

    def window(self, start: datetime, end: datetime) -> Fingerprint:
        for fingerprint in self._history:
            if fingerprint.window_start == start and fingerprint.window_end == end:
                return fingerprint
        raise ValueError(f"no telemetry window recorded for [{start}, {end})")

    def series(self, start: datetime, end: datetime, step_s: int = WINDOW_S) -> list[Fingerprint]:
        if step_s != WINDOW_S:
            raise ValueError(f"C1 polling requires exactly {WINDOW_S}s windows")
        return [fp for fp in self._history if fp.window_start >= start and fp.window_end <= end]
