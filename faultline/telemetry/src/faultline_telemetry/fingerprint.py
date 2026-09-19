"""C1 fingerprint assembly from two sandbox `/stats` snapshots."""

from datetime import datetime
from typing import Any

from faultline_contracts import DbStats, Edge, Fingerprint, ServiceStats, SloStatus


def _delta(a: dict[str, Any], b: dict[str, Any], key: str) -> float:
    return float(b.get("counters", {}).get(key, 0)) - float(a.get("counters", {}).get(key, 0))


def _ratio(a: float, b: float) -> float | None:
    return a / b if b > 0 else None


def _hist(a: dict[str, Any], b: dict[str, Any], key: str) -> list[int] | None:
    cur = b.get("hists", {}).get(key)
    if cur is None:
        return None
    prev = a.get("hists", {}).get(key, {"counts": [0] * len(cur["counts"])})
    return [x - y for x, y in zip(cur["counts"], prev["counts"])]


def _quantile(counts: list[int] | None, buckets: list[float], q: float) -> float | None:
    if not counts or sum(counts) <= 0:
        return None
    rank, seen = q * sum(counts), 0
    for i, count in enumerate(counts):
        if count and seen + count >= rank:
            lo = buckets[i - 1] if i else 0.0
            hi = buckets[i] if i < len(buckets) else buckets[-1] * 2
            return lo + (hi - lo) * (rank - seen) / count
        seen += count
    return buckets[-1] * 2


def fingerprint_from_stats(previous: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]], start: datetime, end: datetime) -> Fingerprint:
    """Build one C1 window. Missing inputs stay omitted; no hidden-state data is used."""
    orders0, orders1 = previous["orders"], current["orders"]
    payments0, payments1 = previous["payments"], current["payments"]
    load0, load1 = previous["loadgen"], current["loadgen"]
    dt = max(1e-6, float(orders1["t"]) - float(orders0["t"]))
    buckets = list(orders1["buckets_ms"])
    req, attempts = _delta(orders0, orders1, "requests"), _delta(orders0, orders1, "attempts")
    ok, errors = _delta(orders0, orders1, "ok"), _delta(orders0, orders1, "errors")
    sent, gw_errors = _delta(load0, load1, "sent"), _delta(load0, load1, "errors")
    issued = _delta(payments0, payments1, "db_queries_issued")
    pool = float(payments1.get("gauges", {}).get("pool_size") or 1)
    db_p50 = _quantile(_hist(payments0, payments1, "db_query"), buckets, .5)
    db_p99 = _quantile(_hist(payments0, payments1, "db_query"), buckets, .99)
    orders_p50 = _quantile(_hist(orders0, orders1, "request"), buckets, .5)
    orders_p99 = _quantile(_hist(orders0, orders1, "request"), buckets, .99)
    attempt_p99 = _quantile(_hist(orders0, orders1, "attempt"), buckets, .99)
    gateway_p99 = _quantile(_hist(load0, load1, "request"), buckets, .99)
    db = DbStats(qps=issued / dt, query_p50_ms=db_p50, query_p99_ms=db_p99,
                 pool_busy_ratio=min(1.0, _delta(payments0, payments1, "db_busy_s") / (dt * pool)))
    services = {
        "gateway": ServiceStats(qps=sent / dt, p99_ms=gateway_p99, error_rate=_ratio(gw_errors, sent)),
        "orders": ServiceStats(qps=req / dt, p50_ms=orders_p50, p99_ms=orders_p99,
            error_rate=_ratio(errors, ok + errors), retry_ratio=_ratio(attempts, req),
            timeout_rate=_ratio(_delta(orders0, orders1, "attempt_timeouts"), attempts)),
        "payments": ServiceStats(qps=_delta(payments0, payments1, "requests") / dt,
            p50_ms=_quantile(_hist(payments0, payments1, "request"), buckets, .5),
            p99_ms=_quantile(_hist(payments0, payments1, "request"), buckets, .99),
            error_rate=_ratio(_delta(payments0, payments1, "errors"), _delta(payments0, payments1, "requests"))),
    }
    edges = [Edge(src="orders", dst="payments", qps=attempts / dt, p99_ms=attempt_p99,
                  error_rate=_ratio(_delta(orders0, orders1, "attempt_timeouts") + _delta(orders0, orders1, "attempt_errors"), attempts)),
             Edge(src="payments", dst="db", qps=issued / dt, p99_ms=db_p99,
                  error_rate=_ratio(_delta(payments0, payments1, "db_errors"), issued))]
    return Fingerprint(window_start=start, window_end=end, services=services, db=db, edges=edges,
        slos=[SloStatus(name="checkout", metric="svc.gateway.p99_ms", threshold=1000, value=gateway_p99, breached=bool(gateway_p99 and gateway_p99 > 1000))])
