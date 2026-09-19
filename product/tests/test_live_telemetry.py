import urllib.error
from datetime import datetime, timezone

import pytest
from faultline_product.adapters import (
    LiveTelemetrySource,
    TelemetryUnavailable,
    fingerprint_from_snapshots,
)
from faultline_product.cli import main

BUCKETS = [10, 100, 1000]


def _hist(counts):
    return {"counts": counts}


def _snapshot(t: float, *, slow=False, empty=False, missing_pool=False):
    multiplier = int(t / 5)
    request_counts = [0, 10 * multiplier, 0, 0]
    slow_request_counts = [0, 10 * multiplier, 0, 10 * multiplier] if slow else request_counts
    if empty:
        request_counts = [0, 0, 0, 0]
        slow_request_counts = request_counts
    orders_requests = 400 * multiplier
    orders_attempts = (800 if slow else 400) * multiplier
    orders = {
        "t": t,
        "buckets_ms": BUCKETS,
        "counters": {
            "requests": orders_requests,
            "attempts": orders_attempts,
            "attempt_errors": 0,
            "retries": orders_attempts - orders_requests,
            "errors": 0,
            "ok": orders_requests,
            "attempt_timeouts": 0,
        },
        "gauges": {
            "max_retries": 3,
            "attempt_timeout_ms": 500,
            "in_flight": 0,
            "version": "v1",
        },
        "hists": {
            "attempt": _hist(request_counts),
            "request": _hist(request_counts),
        },
    }
    payments = {
        "t": t,
        "buckets_ms": BUCKETS,
        "counters": {
            "requests": 400 * multiplier,
            "db_queries_issued": 400 * multiplier,
            "db_busy_s": 40 * multiplier,
            "db_queries_completed": 400 * multiplier,
            "completed_after_client_gone": 0,
        },
        "gauges": {
            "pool_size": 4,
        },
        "hists": {
            "db_query": _hist(request_counts),
            "request": _hist(request_counts),
        },
    }
    if missing_pool:
        payments["gauges"].pop("pool_size")
    loadgen = {
        "t": t,
        "buckets_ms": BUCKETS,
        "counters": {
            "sent": 400 * multiplier,
            "errors": 0,
            "status_503": 0,
            "ok": 400 * multiplier,
        },
        "gauges": {},
        "hists": {"request": _hist(slow_request_counts)},
    }
    return {"orders": orders, "payments": payments, "loadgen": loadgen}


def _scripted_http(snapshots):
    state = {"snapshot": 0}

    def http(url, timeout):
        del timeout
        service = next(name for name in snapshots[0] if name in url)
        snapshot = snapshots[state["snapshot"]]
        if service == "loadgen":
            state["snapshot"] += 1
        return snapshot[service]

    return http


def _source(snapshots):
    return LiveTelemetrySource(
        orders_url="http://orders",
        payments_url="http://payments",
        loadgen_url="http://loadgen",
        http=_scripted_http(snapshots),
    )


def test_fingerprint_derives_live_metrics_and_slo():
    fingerprint = fingerprint_from_snapshots(_snapshot(0), _snapshot(5))

    assert fingerprint.services["gateway"].qps == pytest.approx(80)
    assert fingerprint.services["gateway"].error_rate == 0
    assert fingerprint.services["orders"].retry_ratio == pytest.approx(1)
    assert fingerprint.db.qps == pytest.approx(80)
    assert fingerprint.db.query_p50_ms == pytest.approx(55)
    assert fingerprint.slos[0].metric == "svc.gateway.p99_ms"
    assert fingerprint.slos[0].breached is False
    metrics = fingerprint.metrics()
    assert {
        "svc.gateway.p99_ms",
        "svc.gateway.error_rate",
        "svc.orders.retry_ratio",
        "db.qps",
        "db.query_p50_ms",
    } <= metrics.keys()
    assert "svc.payments.error_rate" not in metrics


def test_empty_histograms_and_missing_pool_are_none():
    fingerprint = fingerprint_from_snapshots(
        _snapshot(0, empty=True, missing_pool=True),
        _snapshot(5, empty=True, missing_pool=True),
    )

    assert fingerprint.services["gateway"].p50_ms is None
    assert fingerprint.services["gateway"].p99_ms is None
    assert fingerprint.services["orders"].p99_ms is None
    assert fingerprint.services["payments"].p50_ms is None
    assert fingerprint.db.query_p99_ms is None
    assert fingerprint.db.pool_busy_ratio is None


def test_window_and_series_use_snapshot_pairs():
    snapshots = [_snapshot(0), _snapshot(5), _snapshot(10)]
    source = _source(snapshots)

    with pytest.raises(ValueError, match="no telemetry covering"):
        source.window(
            datetime.fromtimestamp(0, timezone.utc), datetime.fromtimestamp(5, timezone.utc)
        )
    source.snapshot()
    source.snapshot()
    source.snapshot()

    start = datetime.fromtimestamp(0, timezone.utc)
    middle = datetime.fromtimestamp(5, timezone.utc)
    end = datetime.fromtimestamp(10, timezone.utc)
    assert source.window(middle, end).window_start == middle
    assert len(source.series(start, end, step_s=5)) == 2


def test_wait_for_breach_and_timeout():
    source = _source([_snapshot(0), _snapshot(5), _snapshot(10, slow=True)])
    source.snapshot()
    source.snapshot()
    source.snapshot()

    fingerprint = source.wait_for_breach(timeout_s=1, poll_s=0)
    assert fingerprint.slos[0].breached
    with pytest.raises(TimeoutError):
        source.wait_for_breach(timeout_s=0)


def test_unavailable_http_propagates_and_healthz_is_false():
    def failing_http(url, timeout):
        raise urllib.error.URLError("refused")

    source = LiveTelemetrySource(http=failing_http)
    with pytest.raises(TelemetryUnavailable):
        source.snapshot()
    assert source.healthz() is False


def test_sandbox_telemetry_requires_sandbox_levers(tmp_path, capsys):
    result = main(
        [
            "--audit-log",
            str(tmp_path / "audit.jsonl"),
            "watch",
            "--telemetry",
            "sandbox",
            "--levers",
            "fixture",
            "--incident",
            "pairing",
        ]
    )

    assert result == 2
    assert "--telemetry sandbox requires --levers sandbox" in capsys.readouterr().out
