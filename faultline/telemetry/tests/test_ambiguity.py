from datetime import datetime, timedelta, timezone

from faultline_contracts.fingerprint import Fingerprint, ServiceStats

from faultline_telemetry.ambiguity import ambiguity_rows


def test_ambiguity_export_contains_only_canonical_observable_metrics():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = ambiguity_rows([
        Fingerprint(
            window_start=start,
            window_end=start + timedelta(seconds=5),
            services={"orders": ServiceStats(qps=80, retry_ratio=4)},
        )
    ])
    assert rows == [{
        "window_start": "2026-01-01T00:00:00+00:00",
        "window_end": "2026-01-01T00:00:05+00:00",
        "metrics": {"svc.orders.qps": 80.0, "svc.orders.retry_ratio": 4.0},
    }]
    assert "world" not in str(rows).lower()
    assert "fault" not in str(rows).lower()
