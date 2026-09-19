from datetime import datetime, timedelta, timezone

import pytest

from faultline_telemetry.fingerprint import fingerprint_from_stats
from faultline_telemetry.source import PollingTelemetrySource
from faultline_telemetry.store import ElasticsearchFingerprintStore


def snapshot(t, requests, attempts, issued, *, include_db=True):
    histogram = {"counts": [0, requests, 0] if include_db else [0, 0, 0]}
    orders = {"t": t, "buckets_ms": [10, 100], "counters": {"requests": requests, "attempts": attempts, "ok": requests, "errors": 0, "attempt_timeouts": 0, "attempt_errors": 0}, "gauges": {}, "hists": {"request": histogram, "attempt": histogram}}
    payments = {"t": t, "buckets_ms": [10, 100], "counters": {"requests": attempts, "errors": 0, "db_queries_issued": issued, "db_errors": 0, "db_busy_s": t}, "gauges": {"pool_size": 4}, "hists": {"request": histogram, "db_query": histogram}}
    loadgen = {"t": t, "buckets_ms": [10, 100], "counters": {"sent": requests, "errors": 0}, "gauges": {}, "hists": {"request": histogram}}
    return {"orders": orders, "payments": payments, "loadgen": loadgen}


def test_fingerprint_uses_issued_db_qps_and_omits_nonexistent_fraud_check():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fp = fingerprint_from_stats(snapshot(0, 0, 0, 0), snapshot(5, 400, 800, 800), start, start + timedelta(seconds=5))
    assert fp.db.qps == 160
    assert fp.services["orders"].retry_ratio == 2
    assert "fraud_check" not in fp.services
    assert all("fraud_check" not in (edge.src, edge.dst) for edge in fp.edges)


def test_missing_counter_is_omitted_not_reported_as_zero():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    before, after = snapshot(0, 0, 0, 0), snapshot(5, 10, 10, 10)
    del before["payments"]["counters"]["db_queries_issued"]
    assert fingerprint_from_stats(before, after, start, start + timedelta(seconds=5)).db.qps is None


class FakeStats:
    def __init__(self, values): self.values = iter(values)
    def snapshot(self): return next(self.values)


class FakeElastic:
    def __init__(self): self.docs = []
    def index(self, *, index, document): self.docs.append((index, document))
    def search(self, *, index, query, sort): return {"hits": {"hits": []}}


def test_polling_source_persists_completed_c1_windows_to_elasticsearch():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    elastic = FakeElastic()
    source = PollingTelemetrySource(FakeStats([snapshot(0, 0, 0, 0), snapshot(5, 400, 800, 800)]), ElasticsearchFingerprintStore(elastic))
    assert source.poll(start, start + timedelta(seconds=5)) is None
    fingerprint = source.poll(start + timedelta(seconds=5), start + timedelta(seconds=10))
    assert source.window(fingerprint.window_start, fingerprint.window_end) == fingerprint
    assert elastic.docs[0][0] == "faultline-fingerprints"


def test_poll_requires_exact_contract_window():
    source = PollingTelemetrySource(FakeStats([]))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="exactly 5s"):
        source.poll(start, start + timedelta(seconds=4))
