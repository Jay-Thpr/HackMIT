"""Cumulative counters, gauges and fixed-bucket latency histograms served at GET /stats.

Everything here is ordinary application telemetry. Consumers take deltas between two
snapshots to get rates and window quantiles (see services/common/probe.py).
"""

import os
import time
from bisect import bisect_left
from collections import defaultdict
from typing import Any

from fastapi import Header, HTTPException

BUCKETS_MS = (1, 2, 5, 10, 20, 30, 40, 50, 75, 100, 150, 200, 300, 400, 500, 600, 750,
              1000, 1500, 2000, 2500, 3000, 4000, 5000, 7500, 10000)

INTERNAL_TOKEN = os.environ.get("SANDBOX_TOKEN", "sandbox-internal")


def require_token(x_sandbox_token: str | None = Header(default=None)) -> None:
    """Guard for /internal/* endpoints: only the control plane knows the token."""
    if x_sandbox_token != INTERNAL_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")


class Hist:
    def __init__(self) -> None:
        self.counts = [0] * (len(BUCKETS_MS) + 1)  # last bucket = +Inf
        self.count = 0
        self.sum_ms = 0.0

    def observe(self, ms: float) -> None:
        self.counts[bisect_left(BUCKETS_MS, ms)] += 1
        self.count += 1
        self.sum_ms += ms

    def snapshot(self) -> dict[str, Any]:
        return {"counts": list(self.counts), "count": self.count, "sum_ms": round(self.sum_ms, 3)}


class Stats:
    def __init__(self, service: str) -> None:
        self.service = service
        self.counters: dict[str, float] = defaultdict(float)
        self.hists: dict[str, Hist] = defaultdict(Hist)
        self.gauges: dict[str, Any] = {}

    def inc(self, name: str, n: float = 1) -> None:
        self.counters[name] += n

    def observe(self, name: str, ms: float) -> None:
        self.hists[name].observe(ms)

    def snapshot(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "t": time.time(),
            "buckets_ms": BUCKETS_MS,
            "counters": dict(self.counters),
            "gauges": dict(self.gauges),
            "hists": {k: h.snapshot() for k, h in self.hists.items()},
        }


class RateLimitedLog:
    """Emit at most one line per key per `every_s`, with a count of suppressed repeats."""

    def __init__(self, logger, every_s: float = 1.0) -> None:
        self.logger, self.every_s = logger, every_s
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = defaultdict(int)

    def log(self, level: int, key: str, msg: str, *args) -> None:
        now = time.monotonic()
        if now - self._last.get(key, 0.0) >= self.every_s:
            n = self._suppressed.pop(key, 0)
            self._last[key] = now
            self.logger.log(level, msg + (f" (+{n} similar)" if n else ""), *args)
        else:
            self._suppressed[key] += 1
