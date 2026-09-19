"""Turn two /stats snapshots (loadgen, orders, payments) into one diagnostics row.

Pure functions, no I/O: used by the fault controller (reset waits for a healthy baseline)
and by scripts/diag.py and scripts/validate.py on the host. A window's row is simply
row(snapshot_at_start, snapshot_at_end).
"""

from typing import Any

Snap = dict[str, dict[str, Any]]  # service -> /stats payload


def _delta(prev: dict, cur: dict, name: str) -> float:
    return cur["counters"].get(name, 0.0) - prev["counters"].get(name, 0.0)


def _hist_delta(prev: dict, cur: dict, name: str) -> list[int] | None:
    c = cur["hists"].get(name)
    if c is None:
        return None
    p = prev["hists"].get(name)
    return [a - (p["counts"][i] if p else 0) for i, a in enumerate(c["counts"])]


def quantile(counts: list[int] | None, buckets_ms: list[float], q: float) -> float | None:
    """Linear interpolation inside the bucket that holds the q-quantile. None if no samples."""
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


def row(prev: Snap, cur: Snap) -> dict[str, Any]:
    """Rates and quantiles over [prev, cur]. Missing data is None, never 0."""
    o0, o1 = prev["orders"], cur["orders"]
    p0, p1 = prev["payments"], cur["payments"]
    l0, l1 = prev["loadgen"], cur["loadgen"]
    dt = max(1e-6, o1["t"] - o0["t"])
    b = o1["buckets_ms"]

    req, att = _delta(o0, o1, "requests"), _delta(o0, o1, "attempts")
    ok, err = _delta(o0, o1, "ok"), _delta(o0, o1, "errors")
    lok, lerr = _delta(l0, l1, "ok"), _delta(l0, l1, "errors")
    db_hist = _hist_delta(p0, p1, "db_query")
    pool_size = p1["gauges"].get("pool_size") or 1
    return {
        "dt_s": dt,
        # gateway / user view (loadgen is the client)
        "gw_qps": _delta(l0, l1, "sent") / dt,
        "gw_ok_ratio": _ratio(lok, lok + lerr),
        "gw_p99_ms": quantile(_hist_delta(l0, l1, "request"), b, 0.99),
        # orders: logical requests vs attempts
        "logical_qps": req / dt,
        "attempt_qps": att / dt,
        "retry_ratio": _ratio(att, req),
        "ok_ratio": _ratio(ok, ok + err),
        "errors_qps": err / dt,
        "attempt_timeouts_qps": _delta(o0, o1, "attempt_timeouts") / dt,
        "orders_p50_ms": quantile(_hist_delta(o0, o1, "request"), b, 0.50),
        "orders_p99_ms": quantile(_hist_delta(o0, o1, "request"), b, 0.99),
        "max_retries": o1["gauges"].get("max_retries"),
        # payments / db
        "payments_qps": _delta(p0, p1, "requests") / dt,
        "db_issued_qps": _delta(p0, p1, "db_queries_issued") / dt,
        "db_completed_qps": _delta(p0, p1, "db_queries_completed") / dt,
        "db_p50_ms": quantile(db_hist, b, 0.50),
        "db_p99_ms": quantile(db_hist, b, 0.99),
        "db_acquire_timeouts_qps": _delta(p0, p1, "db_acquire_timeouts") / dt,
        "pool_busy_ratio": min(1.0, _delta(p0, p1, "db_busy_s") / (dt * pool_size)),
        "pool_in_use": p1["gauges"].get("pool_in_use"),
        "pool_waiting": p1["gauges"].get("pool_waiting"),
        "late_completions_qps": _delta(p0, p1, "completed_after_client_gone") / dt,
        "db_target": p1["gauges"].get("db_target"),
    }


def is_healthy(r: dict[str, Any], attempt_timeout_ms: float) -> bool:
    """Healthy baseline: traffic flowing, requests succeed, no amplification, DB p99 under the attempt timeout."""
    return (
        r["logical_qps"] > 0
        and (r["ok_ratio"] or 0.0) >= 0.98
        and (r["retry_ratio"] or 99.0) <= 1.10
        and (r["db_p99_ms"] is not None and r["db_p99_ms"] < attempt_timeout_ms)
    )


def is_incident(r: dict[str, Any]) -> bool:
    return r["logical_qps"] > 0 and (r["ok_ratio"] if r["ok_ratio"] is not None else 0.0) < 0.5


def fmt_header() -> str:
    return (f"{'t':>6} {'gw_qps':>6} {'gw_ok':>6} {'logic':>6} {'attmp':>6} {'retry':>5} {'ok':>5} {'ord99':>6} "
            f"{'db_iss':>6} {'db_ok':>6} {'db_p50':>6} {'db_p99':>6} {'busy':>5} {'wait':>5} {'acqTO':>5} "
            f"{'late':>5} {'maxr':>4} {'dbt':>7}")


def fmt_row(t: float, r: dict[str, Any]) -> str:
    def f(v, w, p=1):
        if v is None:
            return f"{'-':>{w}}"
        if isinstance(v, str):
            return f"{v:>{w}}"
        return f"{v:>{w}.{p}f}"
    return " ".join([
        f(t, 6, 0), f(r["gw_qps"], 6), f(r["gw_ok_ratio"], 6, 2), f(r["logical_qps"], 6), f(r["attempt_qps"], 6),
        f(r["retry_ratio"], 5, 2), f(r["ok_ratio"], 5, 2), f(r["orders_p99_ms"], 6, 0), f(r["db_issued_qps"], 6),
        f(r["db_completed_qps"], 6), f(r["db_p50_ms"], 6, 0), f(r["db_p99_ms"], 6, 0), f(r["pool_busy_ratio"], 5, 2),
        f(r["pool_waiting"], 5, 0), f(r["db_acquire_timeouts_qps"], 5, 0), f(r["late_completions_qps"], 5, 0),
        f(r["max_retries"], 4, 0), f(r["db_target"], 7),
    ])
