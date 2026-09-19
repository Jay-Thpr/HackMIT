from datetime import datetime, timedelta, timezone

from faultline_telemetry.fingerprint import fingerprint_from_stats
from faultline_telemetry.source import PollingTelemetrySource


def snapshot(t, requests, attempts, issued):
    hist = {"counts": [0, requests, 0], "count": requests, "sum_ms": 0}
    return {name: {"t": t, "buckets_ms": [10, 100], "counters": counters, "gauges": gauges, "hists": hists}
            for name, counters, gauges, hists in [
                ("orders", {"requests": requests, "attempts": attempts, "ok": requests, "errors": 0, "attempt_timeouts": 0, "attempt_errors": 0}, {}, {"request": hist, "attempt": hist}),
                ("payments", {"requests": attempts, "errors": 0, "db_queries_issued": issued, "db_errors": 0, "db_busy_s": 5}, {"pool_size": 4}, {"request": hist, "db_query": hist}),
                ("loadgen", {"sent": requests, "errors": 0}, {}, {"request": hist}),
            ]}


def test_fingerprint_uses_issued_db_qps_and_omits_fraud_check():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fp = fingerprint_from_stats(snapshot(0, 0, 0, 0), snapshot(5, 400, 800, 800), start, start + timedelta(seconds=5))
    assert fp.db.qps == 160
    assert fp.services["orders"].retry_ratio == 2
    assert "fraud_check" not in fp.services
    assert all("fraud_check" not in (edge.src, edge.dst) for edge in fp.edges)


class FakeStats:
    def __init__(self, values): self.values = iter(values)
    def snapshot(self): return next(self.values)


def test_polling_source_records_c1_windows_after_its_initial_snapshot():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    source = PollingTelemetrySource(FakeStats([snapshot(0, 0, 0, 0), snapshot(5, 400, 800, 800)]))
    assert source.poll(start, start + timedelta(seconds=5)) is None
    fp = source.poll(start + timedelta(seconds=5), start + timedelta(seconds=10))
    assert source.window(fp.window_start, fp.window_end) == fp
