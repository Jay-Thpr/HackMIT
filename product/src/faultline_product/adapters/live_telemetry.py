import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from faultline_contracts import (
    WINDOW_S,
    DbStats,
    Edge,
    Fingerprint,
    ServiceStats,
    SloStatus,
)

Snapshot = dict[str, dict[str, Any]]


class TelemetryUnavailable(RuntimeError):
    pass


def _delta(prev: dict, cur: dict, name: str) -> float:
    return cur["counters"].get(name, 0.0) - prev["counters"].get(name, 0.0)


def _hist_delta(prev: dict, cur: dict, name: str) -> list[int] | None:
    c = cur["hists"].get(name)
    if c is None:
        return None
    p = prev["hists"].get(name)
    return [a - (p["counts"][i] if p else 0) for i, a in enumerate(c["counts"])]


def quantile(counts: list[int] | None, buckets_ms: list[float], q: float) -> float | None:
    if not counts:
        return None
    total = sum(counts)
    if total <= 0:
        return None
    rank, seen = q * total, 0
    for i, n in enumerate(counts):
        if n and seen + n >= rank:
            lo = buckets_ms[i - 1] if i > 0 else 0.0
            hi = buckets_ms[i] if i < len(buckets_ms) else buckets_ms[-1] * 2
            return lo + (hi - lo) * (rank - seen) / n
        seen += n
    return buckets_ms[-1] * 2


def _ratio(a: float, b: float) -> float | None:
    return a / b if b > 0 else None


def _utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def fingerprint_from_snapshots(prev: Snapshot, cur: Snapshot) -> Fingerprint:
    previous_orders, current_orders = prev["orders"], cur["orders"]
    previous_payments, current_payments = prev["payments"], cur["payments"]
    previous_loadgen, current_loadgen = prev["loadgen"], cur["loadgen"]
    window_start = _utc(previous_orders["t"])
    window_end = _utc(current_orders["t"])
    dt = max(1e-6, (window_end - window_start).total_seconds())
    orders_buckets = current_orders["buckets_ms"]
    payments_buckets = current_payments["buckets_ms"]

    loadgen_errors = _delta(previous_loadgen, current_loadgen, "errors")
    loadgen_ok = _delta(previous_loadgen, current_loadgen, "ok")
    gateway_p50 = quantile(
        _hist_delta(previous_loadgen, current_loadgen, "request"),
        orders_buckets,
        0.50,
    )
    gateway_p99 = quantile(
        _hist_delta(previous_loadgen, current_loadgen, "request"),
        orders_buckets,
        0.99,
    )
    orders_requests = _delta(previous_orders, current_orders, "requests")
    orders_attempts = _delta(previous_orders, current_orders, "attempts")
    orders_errors = _delta(previous_orders, current_orders, "errors")
    orders_ok = _delta(previous_orders, current_orders, "ok")
    payment_requests = _delta(previous_payments, current_payments, "requests")
    db_queries_issued = _delta(previous_payments, current_payments, "db_queries_issued")
    pool_size = current_payments["gauges"].get("pool_size")
    pool_busy_ratio = (
        min(
            1.0,
            _ratio(
                _delta(previous_payments, current_payments, "db_busy_s"),
                dt * pool_size,
            ),
        )
        if pool_size
        else None
    )

    gateway = ServiceStats(
        qps=_delta(previous_loadgen, current_loadgen, "sent") / dt,
        p50_ms=gateway_p50,
        p99_ms=gateway_p99,
        error_rate=_ratio(loadgen_errors, loadgen_ok + loadgen_errors),
    )
    orders = ServiceStats(
        qps=orders_requests / dt,
        p50_ms=quantile(
            _hist_delta(previous_orders, current_orders, "request"),
            orders_buckets,
            0.50,
        ),
        p99_ms=quantile(
            _hist_delta(previous_orders, current_orders, "request"),
            orders_buckets,
            0.99,
        ),
        error_rate=_ratio(orders_errors, orders_ok + orders_errors),
        retry_ratio=_ratio(orders_attempts, orders_requests),
        timeout_rate=_ratio(
            _delta(previous_orders, current_orders, "attempt_timeouts"),
            orders_attempts,
        ),
    )
    payments = ServiceStats(
        qps=payment_requests / dt,
        p50_ms=quantile(
            _hist_delta(previous_payments, current_payments, "request"),
            payments_buckets,
            0.50,
        ),
        p99_ms=quantile(
            _hist_delta(previous_payments, current_payments, "request"),
            payments_buckets,
            0.99,
        ),
    )
    db = DbStats(
        qps=db_queries_issued / dt,
        query_p50_ms=quantile(
            _hist_delta(previous_payments, current_payments, "db_query"),
            payments_buckets,
            0.50,
        ),
        query_p99_ms=quantile(
            _hist_delta(previous_payments, current_payments, "db_query"),
            payments_buckets,
            0.99,
        ),
        pool_busy_ratio=pool_busy_ratio,
    )
    edges = [
        Edge(
            src="orders",
            dst="payments",
            qps=orders_attempts / dt,
            p99_ms=quantile(
                _hist_delta(previous_orders, current_orders, "attempt"),
                orders_buckets,
                0.99,
            ),
            error_rate=_ratio(
                _delta(previous_orders, current_orders, "attempt_timeouts")
                + _delta(previous_orders, current_orders, "attempt_errors"),
                orders_attempts,
            ),
        ),
        Edge(
            src="payments",
            dst="db",
            qps=db_queries_issued / dt,
            p99_ms=quantile(
                _hist_delta(previous_payments, current_payments, "db_query"),
                payments_buckets,
                0.99,
            ),
        ),
    ]
    slos = [
        SloStatus(
            name="checkout",
            metric="svc.gateway.p99_ms",
            threshold=1000.0,
            value=gateway_p99,
            breached=gateway_p99 is not None and gateway_p99 > 1000.0,
        )
    ]
    return Fingerprint(
        window_start=window_start,
        window_end=window_end,
        services={"gateway": gateway, "orders": orders, "payments": payments},
        db=db,
        edges=edges,
        slos=slos,
    )


def _get_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.URLError as exc:
        raise TelemetryUnavailable(f"telemetry unavailable at {url}") from exc


class LiveTelemetrySource:
    def __init__(
        self,
        orders_url: str = "http://localhost:8101",
        payments_url: str = "http://localhost:8102",
        loadgen_url: str = "http://localhost:8103",
        timeout_s: float = 3.0,
        retain_s: float = 1800,
        http: Callable[[str, float], dict] = _get_json,
    ):
        self._urls = {
            "orders": orders_url.rstrip("/") + "/stats",
            "payments": payments_url.rstrip("/") + "/stats",
            "loadgen": loadgen_url.rstrip("/") + "/stats",
        }
        self._timeout_s = timeout_s
        self._retain_s = retain_s
        self._http = http
        self._snapshots: deque[tuple[datetime, Snapshot]] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def snapshot(self) -> Snapshot:
        snapshot: Snapshot = {}
        try:
            for service, url in self._urls.items():
                snapshot[service] = self._http(url, self._timeout_s)
        except urllib.error.URLError as exc:
            raise TelemetryUnavailable(f"telemetry unavailable at {url}") from exc
        timestamp = _utc(snapshot["orders"]["t"])
        with self._lock:
            self._snapshots.append((timestamp, snapshot))
            cutoff = timestamp - timedelta(seconds=self._retain_s)
            while self._snapshots and self._snapshots[0][0] < cutoff:
                self._snapshots.popleft()
        return snapshot

    def start(self, period_s: float = WINDOW_S) -> None:
        if period_s <= 0:
            raise ValueError("period_s must be positive")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(period_s,),
                daemon=True,
                name="faultline-live-telemetry",
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop.set()
        thread.join()
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def window(self, start: datetime, end: datetime) -> Fingerprint:
        with self._lock:
            entries = list(self._snapshots)
        if len(entries) < 2:
            raise ValueError(f"no telemetry covering [{start}, {end})")
        previous_index = next(
            (index for index in range(len(entries) - 1, -1, -1) if entries[index][0] <= start),
            0,
        )
        current_index = next(
            (index for index in range(len(entries) - 1, -1, -1) if entries[index][0] <= end),
            None,
        )
        if current_index is None or previous_index == current_index:
            raise ValueError(f"no telemetry covering [{start}, {end})")
        return fingerprint_from_snapshots(
            entries[previous_index][1],
            entries[current_index][1],
        )

    def series(
        self,
        start: datetime,
        end: datetime,
        step_s: int = WINDOW_S,
    ) -> list[Fingerprint]:
        if step_s <= 0:
            raise ValueError("step_s must be positive")
        step = timedelta(seconds=step_s)
        fingerprints = []
        current = start
        while current + step <= end + timedelta(seconds=0.5):
            try:
                fingerprints.append(self.window(current, current + step))
            except ValueError:
                pass
            current += step
        return fingerprints

    def latest(self) -> Fingerprint | None:
        with self._lock:
            if len(self._snapshots) < 2:
                return None
            previous, current = list(self._snapshots)[-2:]
        return fingerprint_from_snapshots(previous[1], current[1])

    def wait_for_breach(self, timeout_s: float, poll_s: float = 1.0) -> Fingerprint:
        if timeout_s <= 0:
            raise TimeoutError(f"no SLO breach observed within {timeout_s}s")
        deadline = time.monotonic() + timeout_s
        while True:
            fingerprint = self.latest()
            if fingerprint is not None and any(slo.breached for slo in fingerprint.slos):
                return fingerprint
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no SLO breach observed within {timeout_s}s")
            self._stop.wait(min(max(poll_s, 0.01), remaining))

    def healthz(self) -> bool:
        try:
            for url in self._urls.values():
                self._fetch(url)
        except TelemetryUnavailable:
            return False
        return True

    def _fetch(self, url: str) -> dict:
        try:
            return self._http(url, self._timeout_s)
        except urllib.error.URLError as exc:
            raise TelemetryUnavailable(f"telemetry unavailable at {url}") from exc

    def _run(self, period_s: float) -> None:
        while not self._stop.is_set():
            try:
                self.snapshot()
            except TelemetryUnavailable:
                pass
            self._stop.wait(period_s)
