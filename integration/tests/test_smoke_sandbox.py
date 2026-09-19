"""Runs the full sandbox smoke sequence against a live stack. Skipped unless the sandbox is reachable.

  cd integration && uv run pytest -q          # ~6 min with the stack up; skips otherwise
"""

import sys
from pathlib import Path

import httpx
import pytest

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
    assert m["svc.orders.qps"] == 0.0
    assert m["svc.orders.error_rate"] is None
    assert m["svc.orders.retry_ratio"] is None
    assert m["db.query_p99_ms"] is None
    assert m["db.pool_busy_ratio"] is None
    assert not sm.is_healthy(m)


def test_quantile_interpolates_and_treats_last_bucket_as_inf():
    assert sm.quantile([0, 10, 0], [10, 20], 0.5) == pytest.approx(15.0)
    assert 20.0 < sm.quantile([0, 0, 4], [10, 20], 0.99) <= 40.0
    assert sm.quantile([0, 0, 0], [10, 20], 0.5) is None


@pytest.mark.skipif(not _up(), reason="sandbox not running on :9900/:9901")
def test_hero_sequence_end_to_end(tmp_path):
    report = sm.Smoke(verbose=False).run()
    sm.write_report(report, tmp_path / "smoke.json")
    assert report.aborted is None, report.aborted
    assert not report.failed, "\n".join(f"[{c.step}] {c.name}: {c.detail}" for c in report.failed)
