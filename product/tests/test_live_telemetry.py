import logging
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


@pytest.fixture(autouse=True)
def isolate_cloud_persistence(monkeypatch):
    monkeypatch.setattr("faultline_product.cli.load_repo_dotenv", lambda _: None)
    monkeypatch.delenv("FAULTLINE_ELASTICSEARCH_URL", raising=False)
    monkeypatch.delenv("FAULTLINE_ELASTICSEARCH_API_KEY", raising=False)


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


def test_fingerprint_includes_optional_orders_v2_metrics():
    previous = _snapshot(0)
    current = _snapshot(5)
    previous["orders_v2"] = _snapshot(0)["orders"]
    current["orders_v2"] = _snapshot(5)["orders"]

    fingerprint = fingerprint_from_snapshots(previous, current)

    assert fingerprint.services["orders_v2"].qps == pytest.approx(80)
    assert fingerprint.services["orders_v2"].error_rate == 0


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


def test_fingerprint_matches_owner2_builder_and_omits_missing_counters():
    from faultline_telemetry.fingerprint import fingerprint_from_stats

    previous, current = _snapshot(0), _snapshot(5)
    start, end = datetime.fromtimestamp(0, timezone.utc), datetime.fromtimestamp(5, timezone.utc)
    assert fingerprint_from_snapshots(previous, current, start, end) == fingerprint_from_stats(
        previous, current, start, end
    )

    del previous["orders"]["counters"]["attempts"], current["orders"]["counters"]["attempts"]
    fingerprint = fingerprint_from_snapshots(previous, current)
    assert fingerprint.services["orders"].retry_ratio is None
    assert "svc.orders.retry_ratio" not in fingerprint.metrics()


class RecordingWriter:
    def __init__(self):
        self.writes = []

    def write(self, fingerprint, *, incident_id=None, clone_id=None):
        self.writes.append((fingerprint, incident_id, clone_id))


def test_live_source_persists_each_window_once_with_incident_metadata():
    writer = RecordingWriter()
    source = LiveTelemetrySource(
        orders_url="http://orders",
        payments_url="http://payments",
        loadgen_url="http://loadgen",
        http=_scripted_http([_snapshot(0), _snapshot(5)]),
        writer=writer,
        incident_id="incident-7",
    )
    source.snapshot()
    source.snapshot()
    start = datetime.fromtimestamp(0, timezone.utc)
    end = datetime.fromtimestamp(5, timezone.utc)
    assert source.window(start, end).window_start == start
    assert source.window(start, end).window_start == start
    assert [(item[1], item[2]) for item in writer.writes] == [("incident-7", None)]


def test_persistence_failure_is_sanitized_and_retried_with_clone_metadata(caplog):
    from unittest.mock import Mock

    writer = Mock()
    writer.write.side_effect = [RuntimeError("secret URL credential"), None, None]
    source = LiveTelemetrySource(
        orders_url="http://orders", payments_url="http://payments", loadgen_url="http://loadgen",
        http=_scripted_http([_snapshot(0), _snapshot(5), _snapshot(10)]),
        writer=writer, incident_id="incident-7", clone_id="clone-7",
    )
    source.snapshot()
    source.snapshot()
    first = source.latest()
    assert first is not None
    assert "RuntimeError" in caplog.text and "secret" not in caplog.text
    assert source.latest() == first
    source.latest()
    assert writer.write.call_count == 2
    source.snapshot()
    assert source.latest().window_end > first.window_end
    assert writer.write.call_count == 3
    assert all(call.kwargs == {"incident_id": "incident-7", "clone_id": "clone-7"}
               for call in writer.write.call_args_list)

def test_persist_failure_is_logged_and_retried_without_stopping_polling(caplog):
    class FailingWriter:
        def __init__(self):
            self.calls = 0

        def write(self, fingerprint, *, incident_id=None, clone_id=None):
            self.calls += 1
            raise RuntimeError("es down")

    writer = FailingWriter()
    source = LiveTelemetrySource(
        orders_url="http://orders",
        payments_url="http://payments",
        loadgen_url="http://loadgen",
        http=_scripted_http([_snapshot(0), _snapshot(5)]),
        writer=writer,
    )
    source.snapshot()
    source.snapshot()
    start = datetime.fromtimestamp(0, timezone.utc)
    end = datetime.fromtimestamp(5, timezone.utc)

    with caplog.at_level(logging.WARNING):
        fingerprint = source.window(start, end)  # a down ES must not break reads

    assert fingerprint.window_start == start
    assert writer.calls == 1
    assert "fingerprint persist failed" in caplog.text
    assert not source._persisted_windows  # the window stays eligible for a retry

    again = source.window(start, end)  # reads keep working and the write is retried
    assert again == fingerprint
    assert writer.calls == 2


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


def test_wait_for_breach_requires_sustained_breach():
    # healthy, breached, healthy, breached, breached: one transient window must not count
    source = _source(
        [_snapshot(0), _snapshot(5, slow=True), _snapshot(10), _snapshot(15, slow=True), _snapshot(20, slow=True)]
    )
    source.snapshot()
    source.snapshot()
    assert source.latest().slos[0].breached
    with pytest.raises(TimeoutError):
        source.wait_for_breach(timeout_s=0.05, poll_s=0.01, sustain_s=10)
    source.snapshot()  # healthy again: the breach clock must restart
    source.snapshot()
    source.snapshot()
    fingerprint = source.wait_for_breach(timeout_s=1, poll_s=0.01, sustain_s=0.05)
    assert fingerprint.slos[0].breached


def test_connection_reset_is_unavailable_and_optional_v2_is_skipped(monkeypatch):
    import http.client

    from faultline_product.adapters import live_telemetry

    def urlopen(request, timeout):
        raise http.client.RemoteDisconnected("closed without response")

    monkeypatch.setattr(live_telemetry.urllib.request, "urlopen", urlopen)
    with pytest.raises(TelemetryUnavailable):
        live_telemetry._get_json("http://orders-v2/stats", 3)

    def http(url, timeout):
        if "orders_v2" in url:
            raise TelemetryUnavailable(url)
        service = next(name for name in ("orders", "payments", "loadgen") if name in url)
        return _snapshot(5)[service]

    source = LiveTelemetrySource(
        orders_url="http://orders",
        payments_url="http://payments",
        loadgen_url="http://loadgen",
        orders_v2_url="http://orders_v2",
        http=http,
    )
    assert "orders_v2" not in source.snapshot()


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
    assert "sandbox telemetry and levers must be selected together" in capsys.readouterr().out


def test_sandbox_levers_require_sandbox_telemetry(tmp_path, capsys):
    result = main(
        [
            "--audit-log",
            str(tmp_path / "audit.jsonl"),
            "watch",
            "--telemetry",
            "fixture",
            "--levers",
            "sandbox",
            "--incident",
            "pairing",
        ]
    )

    assert result == 2
    assert "sandbox telemetry and levers must be selected together" in capsys.readouterr().out


def test_sandbox_profile_refuses_fixture_brain(tmp_path, capsys):
    result = main(
        [
            "--audit-log",
            str(tmp_path / "audit.jsonl"),
            "watch",
            "--telemetry",
            "sandbox",
            "--levers",
            "sandbox",
            "--brain",
            "fixture",
            "--incident",
            "unsafe-brain",
        ]
    )

    assert result == 2
    assert "sandbox mode requires --brain live" in capsys.readouterr().out


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class _FakeStop:
    def __init__(self, clock, interrupt_after=None):
        self.clock = clock
        self.interrupt_after = interrupt_after
        self.waits = 0

    def wait(self, seconds):
        self.waits += 1
        self.clock.now += seconds
        return self.interrupt_after is not None and self.waits > self.interrupt_after

    def is_set(self):
        return False

    def set(self):
        pass

    def clear(self):
        pass


def _canary_snapshot(t: float, v1: float = 76, v2: float = 4):
    multiplier = int(t / 5)
    snap = _snapshot(t)
    orders = snap["orders"]
    for key in ("requests", "attempts", "ok"):
        orders["counters"][key] = v1 * 5 * multiplier
    orders["counters"]["retries"] = 0
    orders["hists"] = {
        "request": _hist([0, v1 * 5 * multiplier, 0, 0]),
        "attempt": _hist([0, v1 * 5 * multiplier, 0, 0]),
    }
    v2stats = {
        "t": t,
        "buckets_ms": BUCKETS,
        "counters": {**orders["counters"]},
        "gauges": {"version": "v2"},
        "hists": {},
    }
    for key in ("requests", "attempts", "ok"):
        v2stats["counters"][key] = v2 * 5 * multiplier
    v2stats["counters"]["retries"] = 0
    v2stats["hists"] = {
        "request": _hist([0, v2 * 5 * multiplier, 0, 0]),
        "attempt": _hist([0, v2 * 5 * multiplier, 0, 0]),
    }
    snap["orders_v2"] = v2stats
    return snap


def _observing_source(snapshots, monkeypatch, writer=None):
    from faultline_product.adapters import live_telemetry

    clock = _FakeClock()
    monkeypatch.setattr(live_telemetry.time, "monotonic", clock.monotonic)
    source = LiveTelemetrySource(
        orders_url="http://orders",
        payments_url="http://payments",
        loadgen_url="http://loadgen",
        orders_v2_url="http://orders_v2",
        http=lambda url, timeout: {},
        writer=writer,
        clone_id="clone-9",
    )
    source._stop = _FakeStop(clock)
    staged = iter(snapshots)
    monkeypatch.setattr(source, "snapshot", lambda: next(staged))
    return source


def test_observe_collects_fresh_phase_anchored_windows(monkeypatch):
    writer = RecordingWriter()
    staged = [_canary_snapshot(5.01 + 5 * i) for i in range(25)]
    source = _observing_source(staged, monkeypatch, writer=writer)
    source._snapshots.append(
        (datetime.fromtimestamp(5.0, timezone.utc), _canary_snapshot(5.0, v1=80, v2=0))
    )

    windows = source.observe(120, required_services=("orders_v2",))

    assert len(windows) == 24
    for fp in windows:
        assert (fp.window_end - fp.window_start).total_seconds() == 5
        assert fp.services["orders_v2"].qps == pytest.approx(4)
        assert fp.services["orders"].qps == pytest.approx(76)
        assert fp.services["orders_v2"].p99_ms is not None
        assert fp.services["orders_v2"].error_rate == 0
        assert fp.services["gateway"].qps == pytest.approx(80)
    assert [(item[1], item[2]) for item in writer.writes] == [(None, "clone-9")] * 24


def _raw_v2(t: float, requests: int, ok: int, hist_count: int):
    return {
        "t": t,
        "buckets_ms": BUCKETS,
        "counters": {
            "requests": requests,
            "attempts": requests,
            "ok": ok,
            "errors": 0,
            "attempt_errors": 0,
            "retries": 0,
            "attempt_timeouts": 0,
        },
        "gauges": {"version": "v2"},
        "hists": {
            "request": _hist([0, hist_count, 0, 0]),
            "attempt": _hist([0, hist_count, 0, 0]),
        },
    }


def test_floor_window_over_midphase_offsets_is_incomplete():
    source = LiveTelemetrySource(
        orders_url="http://orders", payments_url="http://payments",
        loadgen_url="http://loadgen", orders_v2_url="http://orders_v2",
        http=lambda url, timeout: {},
    )
    at_zero = _canary_snapshot(0, v1=0, v2=0)
    at_first = _canary_snapshot(5.01, v1=76, v2=0)
    at_first["orders_v2"] = _raw_v2(5.01, requests=5, ok=0, hist_count=0)
    at_second = _canary_snapshot(10.02, v1=76, v2=0)
    at_second["orders_v2"] = _raw_v2(10.02, requests=25, ok=25, hist_count=25)
    for t, snap in ((0, at_zero), (5.01, at_first), (10.02, at_second)):
        source._snapshots.append((datetime.fromtimestamp(t, timezone.utc), snap))

    start = datetime.fromtimestamp(5.009, timezone.utc)
    end = datetime.fromtimestamp(10.009, timezone.utc)
    fp = source.window(start, end)
    v2 = fp.services["orders_v2"]
    assert v2.qps == pytest.approx(5 / 5.01)
    assert v2.p99_ms is None
    assert v2.error_rate is None


def test_observe_requires_present_and_advancing_required_services(monkeypatch):
    staged = [_canary_snapshot(5.01), _canary_snapshot(10.01)]
    del staged[1]["orders_v2"]
    source = _observing_source(staged, monkeypatch)
    with pytest.raises(TelemetryUnavailable, match="required service absent"):
        source.observe(5, required_services=("orders_v2",))

    static = _canary_snapshot(15.01)
    static["orders_v2"]["t"] = 10.01
    source = _observing_source([_canary_snapshot(5.01), _canary_snapshot(10.01), static],
                               monkeypatch)
    with pytest.raises(TelemetryUnavailable, match="did not advance"):
        source.observe(10, required_services=("orders_v2",))

    missing_start = [_canary_snapshot(5.01)]
    del missing_start[0]["orders_v2"]
    source = _observing_source(missing_start, monkeypatch)
    with pytest.raises(TelemetryUnavailable, match="absent at observation start"):
        source.observe(5, required_services=("orders_v2",))


def test_observe_zero_traffic_does_not_synthesize_healthy(monkeypatch):
    staged = [_canary_snapshot(5.01 + 5 * i, v1=0, v2=0) for i in range(3)]
    source = _observing_source(staged, monkeypatch)
    windows = source.observe(10, required_services=("orders_v2",))
    assert len(windows) == 2
    assert windows[0].services["orders_v2"].qps == 0
    assert windows[0].services["orders_v2"].p99_ms is None


def test_observe_interrupted_stop_raises(monkeypatch):
    source = _observing_source([_canary_snapshot(5.01), _canary_snapshot(10.01)], monkeypatch)
    source._stop.interrupt_after = 0
    with pytest.raises(TelemetryUnavailable, match="interrupted"):
        source.observe(10, required_services=("orders_v2",))


def test_observe_rejects_bad_duration(monkeypatch):
    source = _observing_source([_canary_snapshot(5.01)], monkeypatch)
    for bad in (0, -5, 7, 5.0, True):
        with pytest.raises(ValueError):
            source.observe(bad)


def test_snapshot_calls_are_serialized(monkeypatch):
    import threading

    source = LiveTelemetrySource(
        orders_url="http://orders", payments_url="http://payments",
        loadgen_url="http://loadgen", http=_scripted_http([_snapshot(0)]),
    )
    entered = threading.Event()
    proceed = threading.Event()
    original = source._snapshot

    def guarded():
        entered.set()
        assert proceed.wait(2)
        return original()

    monkeypatch.setattr(source, "_snapshot", guarded)
    with source._scrape_lock:
        worker = threading.Thread(target=source.snapshot, daemon=True)
        worker.start()
        assert entered.wait(1) is False
    proceed.set()
    worker.join(2)
    assert not worker.is_alive()
