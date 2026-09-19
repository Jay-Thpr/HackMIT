"""Public-sandbox polling source for C1 windows, optionally persisted to ES."""

from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx

from faultline_contracts.common import WINDOW_S
from faultline_contracts.fingerprint import Fingerprint

from .fingerprint import fingerprint_from_stats


class FingerprintWriter(Protocol):
    def write(self, fingerprint: Fingerprint) -> None: ...


class HttpSandboxStats:
    """Fetch only public ``/stats`` endpoints; never internal or fault-controller data."""

    def __init__(self, urls: dict[str, str], client: httpx.Client | None = None):
        self._urls = dict(urls)
        self._client = client or httpx.Client(timeout=5.0)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {name: self._client.get(url).raise_for_status().json() for name, url in self._urls.items()}


class PollingTelemetrySource:
    """Turn consecutive snapshots into fixed five-second C1 windows.

    The first poll establishes the baseline. Each subsequent completed window is
    retained for C1 reads and, when provided, written to Elasticsearch.
    """

    def __init__(self, stats: Any, writer: FingerprintWriter | None = None):
        self._stats = stats
        self._writer = writer
        self._previous: dict[str, dict[str, Any]] | None = None
        self._history: list[Fingerprint] = []

    def poll(self, start: datetime, end: datetime) -> Fingerprint | None:
        if end - start != timedelta(seconds=WINDOW_S):
            raise ValueError(f"C1 polling requires exactly {WINDOW_S}s windows")
        current = self._stats.snapshot()
        if self._previous is None:
            self._previous = current
            return None
        fingerprint = fingerprint_from_stats(self._previous, current, start, end)
        self._previous = current
        self._history.append(fingerprint)
        if self._writer is not None:
            self._writer.write(fingerprint)
        return fingerprint

    def window(self, start: datetime, end: datetime) -> Fingerprint:
        matches = [fp for fp in self._history if fp.window_start == start and fp.window_end == end]
        if len(matches) != 1:
            raise ValueError(f"expected one telemetry window for [{start}, {end}), got {len(matches)}")
        return matches[0]

    def series(self, start: datetime, end: datetime, step_s: int = WINDOW_S) -> list[Fingerprint]:
        if step_s != WINDOW_S:
            raise ValueError(f"C1 requires {WINDOW_S}s windows")
        return [fp for fp in self._history if fp.window_start >= start and fp.window_end <= end]
