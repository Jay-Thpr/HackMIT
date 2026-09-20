"""Runs the full sandbox smoke sequence against a live stack. Skipped unless the sandbox is reachable.

  cd integration && uv run pytest -q          # ~6 min with the stack up; skips otherwise
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from faultline_telemetry.fingerprint import fingerprint_from_stats

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import smoke_sandbox as sm  # noqa: E402


def _up() -> bool:
    try:
        return (httpx.get(f"{sm.CONTROL_URL}/healthz", timeout=2).status_code == 200
                and httpx.get(f"{sm.FAULT_URL}/fault/state", timeout=2).status_code == 200)
    except httpx.HTTPError:
        return False


def test_window_metrics_omits_missing_data():
    empty = {"t": 0.0, "buckets_ms": [1, 2], "counters": {}, "gauges": {}, "hists": {}}
    snap = {"orders": empty, "payments": empty, "loadgen": empty}
    later = {k: {**v, "t": 5.0} for k, v in snap.items()}
    m = sm.window_metrics(snap, later)
    assert "svc.orders.qps" not in m
    assert "svc.orders.error_rate" not in m
    assert "svc.orders.retry_ratio" not in m
    assert "db.query_p99_ms" not in m
    assert "db.pool_busy_ratio" not in m
    assert not sm.is_healthy(m)


def test_window_metrics_matches_canonical_fingerprint_metrics():
    counters = {
        "requests": 10,
        "ok": 9,
        "errors": 1,
        "attempts": 11,
        "sent": 10,
        "db_queries_issued": 10,
    }
    previous = {
        name: {"t": 0.0, "buckets_ms": [10.0, 20.0], "counters": counters, "gauges": {}, "hists": {}}
        for name in ("orders", "payments", "loadgen")
    }
    current = {
        name: {**snapshot, "t": 5.0, "counters": {key: value + 10 for key, value in snapshot["counters"].items()}}
        for name, snapshot in previous.items()
    }
    expected = fingerprint_from_stats(
        previous, current, datetime.fromtimestamp(0, timezone.utc), datetime.fromtimestamp(5, timezone.utc)
    ).metrics()
    actual = sm.window_metrics(previous, current)
    assert {key: actual[key] for key in expected} == expected


@pytest.mark.skipif(not _up(), reason="sandbox not running on :9900/:9901")
def test_hero_sequence_end_to_end(tmp_path):
    report = sm.Smoke(verbose=False).run()
    sm.write_report(report, tmp_path / "smoke.json")
    assert report.aborted is None, report.aborted
    assert not report.failed, "\n".join(f"[{c.step}] {c.name}: {c.detail}" for c in report.failed)
