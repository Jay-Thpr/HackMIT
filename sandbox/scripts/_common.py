"""Host-side helpers shared by diag.py and validate.py."""

import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # sandbox/ -> `services` package

from services.common import probe  # noqa: E402

STATS_URLS = {
    "loadgen": os.environ.get("LOADGEN_STATS_URL", "http://127.0.0.1:8103"),
    "orders": os.environ.get("ORDERS_STATS_URL", "http://127.0.0.1:8101"),
    "payments": os.environ.get("PAYMENTS_STATS_URL", "http://127.0.0.1:8102"),
}
CONTROL_URL = os.environ.get("CONTROL_URL", "http://127.0.0.1:9901")
FAULT_URL = os.environ.get("FAULT_URL", "http://127.0.0.1:9900")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080")


class Sampler:
    """Snapshots /stats once per call to tick(); keeps history for window rows."""

    def __init__(self, show_hidden: bool = False, out=sys.stdout) -> None:
        self.http = httpx.Client(timeout=5.0)
        self.hist: list[tuple[float, probe.Snap]] = []
        self.t0 = time.monotonic()
        self.show_hidden = show_hidden
        self.out = out
        self._printed_header = 0

    def snapshot(self) -> probe.Snap:
        return {name: self.http.get(f"{u}/stats").json() for name, u in STATS_URLS.items()}

    def tick(self, label: str = "") -> dict[str, Any] | None:
        """Take a snapshot, print the 1 s row, return it."""
        try:
            snap = self.snapshot()
        except (httpx.HTTPError, ValueError) as e:
            print(f"  (stats unavailable: {e})", file=self.out)
            return None
        now = time.monotonic()
        self.hist.append((now, snap))
        if len(self.hist) < 2:
            return None
        r = probe.row(self.hist[-2][1], snap)
        if self._printed_header % 20 == 0:
            print(probe.fmt_header() + ("  hidden" if self.show_hidden else ""), file=self.out)
        self._printed_header += 1
        line = probe.fmt_row(now - self.t0, r)
        if self.show_hidden:
            line += "  " + self.hidden()
        print(f"{line}  {label}", file=self.out, flush=True)
        return r

    def hidden(self) -> str:
        """Fault-controller state. Benchmark/debug output only; never telemetry."""
        try:
            s = self.http.get(f"{FAULT_URL}/fault/state").json()
            return f"[{s['world']}{'*' if s['active'] else ''}]"
        except (httpx.HTTPError, ValueError):
            return "[?]"

    def window(self, seconds: float) -> dict[str, Any] | None:
        """Row over the last `seconds` of history."""
        if len(self.hist) < 2:
            return None
        t_end, end = self.hist[-1]
        start = next((s for t, s in reversed(self.hist) if t_end - t >= seconds), self.hist[0][1])
        return probe.row(start, end)

    def run_for(self, seconds: float, label: str = "") -> list[dict[str, Any]]:
        """Tick once a second for `seconds`; return the 1 s rows."""
        rows, end = [], time.monotonic() + seconds
        nxt = time.monotonic()
        while time.monotonic() < end:
            r = self.tick(label)
            if r:
                rows.append(r)
            nxt += 1.0
            time.sleep(max(0.0, nxt - time.monotonic()))
        return rows
