from datetime import datetime, timedelta, timezone

import pytest
from faultline_contracts import Fingerprint
from faultline_contracts.fingerprint import ResourceStats, ServiceStats

from faultline_product.advanced_policy import baseline_thresholds, recovered


T0 = datetime(2026, 9, 20, tzinfo=timezone.utc)


def windows(n=24):
    return [Fingerprint(window_start=T0 + timedelta(seconds=5 * i),
                        window_end=T0 + timedelta(seconds=5 * (i + 1)),
                        services={"gateway": ServiceStats(p99_ms=100, error_rate=0),
                                  "fulfillment": ServiceStats(p99_ms=200)},
                        resources={"kafka_partition_0": ResourceStats(lag_messages=0)}) for i in range(n)]


def test_thresholds_derive_from_healthy_noise_not_a_fixed_latency():
    reference = windows()
    thresholds = baseline_thresholds(reference)
    assert thresholds["svc.gateway.p99_ms"] == 130
    assert thresholds["svc.fulfillment.p99_ms"] == 260
    assert thresholds["svc.gateway.error_rate"] == 0
    assert thresholds["resource.kafka_partition_0.lag_messages"] == 0
    for fp in reference:
        fp.services["gateway"].p99_ms = 1000
    assert baseline_thresholds(reference)["svc.gateway.p99_ms"] == 1300


def test_missing_baseline_does_not_define_a_threshold():
    reference = windows()
    reference[3].services["fulfillment"].p99_ms = None
    with pytest.raises(ValueError, match="incomplete"):
        baseline_thresholds(reference)


def test_short_or_discontinuous_baseline_is_refused():
    with pytest.raises(ValueError):
        baseline_thresholds(windows(23))
    reference = windows()
    reference[1] = reference[0]
    with pytest.raises(ValueError):
        baseline_thresholds(reference)


def test_recovery_requires_full_measured_window_and_all_recorded_dimensions():
    thresholds = baseline_thresholds(windows())
    assert recovered(windows(12), thresholds) is True
    assert recovered(windows(11), thresholds) is None
    after = windows(12)
    after[4].resources["kafka_partition_0"].lag_messages = 1
    assert recovered(after, thresholds) is False
    after[4].resources = {}
    assert recovered(after, thresholds) is None


def test_fast_errors_do_not_count_as_recovery():
    thresholds = baseline_thresholds(windows())
    after = windows(12)
    after[2].services["gateway"].error_rate = 0.5
    after[2].services["gateway"].p99_ms = 1
    assert recovered(after, thresholds) is False


def test_duplicate_samples_cannot_satisfy_the_recovery_duration():
    after = windows(12)
    after[1] = after[0]
    assert recovered(after, baseline_thresholds(windows())) is None
