"""Build one contract C1 fingerprint from public sandbox stats snapshots.

The adapter deliberately uses only observable, public ``/stats`` data.  In
particular it records issued DB queries and end-to-end DB-query latency, not
completed throughput or execution-only time, which would reveal the hidden
world.
"""

from datetime import datetime
from typing import Any

from faultline_contracts.fingerprint import DbStats, Edge, Fingerprint, ServiceStats, SloStatus


def _counter_delta(before: dict[str, Any], after: dict[str, Any], key: str) -> float | None:
    old = before.get("counters", {}).get(key)
    new = after.get("counters", {}).get(key)
    if old is None or new is None:
        return None
    return float(new) - float(old)


def _rate(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _per_second(value: float | None, seconds: float) -> float | None:
    return value / seconds if value is not None else None


def _hist_delta(before: dict[str, Any], after: dict[str, Any], name: str) -> tuple[list[float], list[float]] | None:
    current = after.get("hists", {}).get(name)
    if current is None:
        return None
    previous = before.get("hists", {}).get(name)
    if previous is None:
        return None
    buckets = after.get("buckets_ms")
    if not isinstance(buckets, list) or len(current.get("counts", [])) != len(buckets) + 1:
        return None
    if len(previous.get("counts", [])) != len(current["counts"]):
        return None
    return list(map(float, buckets)), [float(now) - float(old) for old, now in zip(previous["counts"], current["counts"])]


def _quantile(histogram: tuple[list[float], list[float]] | None, quantile: float) -> float | None:
    if histogram is None:
        return None
    buckets, counts = histogram
    total = sum(counts)
    if total <= 0 or any(count < 0 for count in counts):
        return None
    rank, seen = quantile * total, 0.0
    for index, count in enumerate(counts):
        if count > 0 and seen + count >= rank:
            low = buckets[index - 1] if index else 0.0
            high = buckets[index] if index < len(buckets) else buckets[-1] * 2
            return low + (high - low) * (rank - seen) / count
        seen += count
    return None


def fingerprint_from_stats(
    previous: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]], start: datetime, end: datetime
) -> Fingerprint:
    """Assemble a fixed C1 window from two public snapshots.

    The elapsed time comes from the producer timestamps, which prevents a slow
    poll from fabricating a five-second rate.  The supplied C1 boundaries remain
    the canonical fixed window.
    """
    orders0, orders1 = previous["orders"], current["orders"]
    payments0, payments1 = previous["payments"], current["payments"]
    load0, load1 = previous["loadgen"], current["loadgen"]
    elapsed_s = float(orders1["t"]) - float(orders0["t"])
    if elapsed_s <= 0:
        raise ValueError("stats timestamps must increase between snapshots")

    requests = _counter_delta(orders0, orders1, "requests")
    attempts = _counter_delta(orders0, orders1, "attempts")
    order_ok = _counter_delta(orders0, orders1, "ok")
    order_errors = _counter_delta(orders0, orders1, "errors")
    payment_requests = _counter_delta(payments0, payments1, "requests")
    issued = _counter_delta(payments0, payments1, "db_queries_issued")
    pool_size = payments1.get("gauges", {}).get("pool_size")

    db_p50 = _quantile(_hist_delta(payments0, payments1, "db_query"), 0.50)
    db_p99 = _quantile(_hist_delta(payments0, payments1, "db_query"), 0.99)
    gateway_p50 = _quantile(_hist_delta(load0, load1, "request"), 0.50)
    gateway_p99 = _quantile(_hist_delta(load0, load1, "request"), 0.99)
    orders_p50 = _quantile(_hist_delta(orders0, orders1, "request"), 0.50)
    orders_p99 = _quantile(_hist_delta(orders0, orders1, "request"), 0.99)
    attempt_p99 = _quantile(_hist_delta(orders0, orders1, "attempt"), 0.99)
    busy_seconds = _counter_delta(payments0, payments1, "db_busy_s")
    pool_busy = (
        min(1.0, busy_seconds / (elapsed_s * float(pool_size)))
        if busy_seconds is not None and pool_size not in (None, 0)
        else None
    )

    services = {
        "gateway": ServiceStats(
            qps=_per_second(_counter_delta(load0, load1, "sent"), elapsed_s),
            p50_ms=gateway_p50,
            p99_ms=gateway_p99,
            error_rate=_rate(_counter_delta(load0, load1, "errors"), _counter_delta(load0, load1, "sent")),
        ),
        "orders": ServiceStats(
            qps=_per_second(requests, elapsed_s), p50_ms=orders_p50, p99_ms=orders_p99,
            error_rate=_rate(order_errors, (order_ok or 0) + (order_errors or 0))
            if order_ok is not None and order_errors is not None else None,
            retry_ratio=_rate(attempts, requests),
            timeout_rate=_rate(_counter_delta(orders0, orders1, "attempt_timeouts"), attempts),
        ),
        "payments": ServiceStats(
            qps=_per_second(payment_requests, elapsed_s),
            p50_ms=_quantile(_hist_delta(payments0, payments1, "request"), 0.50),
            p99_ms=_quantile(_hist_delta(payments0, payments1, "request"), 0.99),
            error_rate=_rate(_counter_delta(payments0, payments1, "errors"), payment_requests),
        ),
    }
    edges = [
        Edge(src="orders", dst="payments", qps=_per_second(attempts, elapsed_s), p99_ms=attempt_p99,
             error_rate=_rate(
                 (_counter_delta(orders0, orders1, "attempt_timeouts") or 0) + (_counter_delta(orders0, orders1, "attempt_errors") or 0),
                 attempts,
             )),
        Edge(src="payments", dst="db", qps=_per_second(issued, elapsed_s), p99_ms=db_p99,
             error_rate=_rate(_counter_delta(payments0, payments1, "db_errors"), issued)),
    ]
    return Fingerprint(
        window_start=start, window_end=end, services=services,
        db=DbStats(qps=_per_second(issued, elapsed_s), query_p50_ms=db_p50, query_p99_ms=db_p99, pool_busy_ratio=pool_busy),
        edges=edges,
        slos=[SloStatus(name="checkout", metric="svc.gateway.p99_ms", threshold=1000, value=gateway_p99, breached=bool(gateway_p99 is not None and gateway_p99 > 1000))],
    )
