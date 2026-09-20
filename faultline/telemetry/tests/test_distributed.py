from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from faultline_telemetry.distributed import fingerprint_from_distributed


T0 = datetime(2026, 9, 20, tzinfo=timezone.utc)


def snapshots():
    stats = {"t": 100.0, "counters": {"requests": 10, "attempts": 10, "errors": 0,
                                       "attempt_timeouts": 0},
             "buckets_ms": [10, 100], "hists": {"request": {"counts": [10, 0, 0]}}}
    before = {"instances": {"pod-a:boot-1": {"service": "gateway", "stats": stats}}}
    after = deepcopy(before)
    current = after["instances"]["pod-a:boot-1"]["stats"]
    current["t"] = 105.0
    current["counters"].update(requests=15, attempts=18, errors=1, attempt_timeouts=2)
    current["hists"]["request"]["counts"] = [13, 2, 0]
    after["resources"] = {"kafka_partition_0": {"lag_messages": 5},
                           "shard_0_replica": {"replication_lag_bytes": 32}}
    after["edges"] = [{"src": "gateway", "dst": "api"}]
    return before, after


def build(before=None, after=None, thresholds=None):
    if before is None:
        before, after = snapshots()
    return fingerprint_from_distributed(before, after, T0, T0 + timedelta(seconds=5), thresholds)


def test_rates_are_computed_from_real_counter_deltas_and_producer_elapsed_time():
    fp = build()
    gateway = fp.services["gateway"]
    assert gateway.qps == 1
    assert gateway.error_rate == 0.2
    assert gateway.retry_ratio == 1.6
    assert gateway.timeout_rate == 0.25
    assert gateway.p50_ms == pytest.approx(10 * 2.5 / 3)
    assert gateway.p99_ms == pytest.approx(10 + 90 * 1.95 / 2)
    assert fp.metrics()["resource.kafka_partition_0.lag_messages"] == 5
    assert fp.metrics()["resource.shard_0_replica.replication_lag_bytes"] == 32


def test_multiple_instances_aggregate_counts_not_percentiles():
    before, after = snapshots()
    before["instances"]["pod-b:boot-1"] = deepcopy(before["instances"]["pod-a:boot-1"])
    after["instances"]["pod-b:boot-1"] = deepcopy(after["instances"]["pod-a:boot-1"])
    fp = build(before, after)
    assert fp.services["gateway"].qps == 2
    assert fp.services["gateway"].error_rate == 0.2
    assert fp.services["gateway"].p99_ms == pytest.approx(build().services["gateway"].p99_ms)


@pytest.mark.parametrize("field,value", [("requests", 2), ("requests", float("nan")),
                                        ("errors", -1), ("attempts", float("inf"))])
def test_counter_resets_and_bad_values_never_become_fabricated_improvement(field, value):
    before, after = snapshots()
    after["instances"]["pod-a:boot-1"]["stats"]["counters"][field] = value
    fp = build(before, after)
    if field == "requests":
        assert "gateway" not in fp.services
    else:
        stats = fp.services.get("gateway")
        assert stats is None or getattr(stats, "error_rate" if field == "errors" else "retry_ratio") is None


def test_restarted_instance_does_not_reuse_old_counters():
    before, after = snapshots()
    after["instances"]["pod-a:boot-2"] = after["instances"].pop("pod-a:boot-1")
    assert "gateway" not in build(before, after).services


def test_missing_instance_does_not_look_like_lower_load():
    before, after = snapshots()
    before["instances"]["pod-b"] = deepcopy(before["instances"]["pod-a:boot-1"])
    assert "gateway" not in build(before, after).services


def test_missing_errors_are_omitted_not_zero():
    before, after = snapshots()
    del after["instances"]["pod-a:boot-1"]["stats"]["counters"]["errors"]
    assert build(before, after).services["gateway"].error_rate is None


def test_resource_missing_invalid_values_are_omitted_not_zero():
    before, after = snapshots()
    after["resources"] = {"replica": {"replication_lag_bytes": -1, "cache_hit_ratio": 2,
                                     "outstanding": None, "completed_total": float("nan")}}
    assert build(before, after).resources == {}


def test_zero_is_retained_only_when_actually_observed():
    before, after = snapshots()
    after["resources"] = {"partition": {"lag_messages": 0}}
    assert build(before, after).metrics()["resource.partition.lag_messages"] == 0


def test_thresholds_only_apply_to_present_measurements():
    fp = build(thresholds={"svc.gateway.p99_ms": 50, "resource.missing.lag_messages": 1})
    assert len(fp.slos) == 1
    assert fp.slos[0].breached
    assert fp.slos[0].value == fp.services["gateway"].p99_ms


def test_stale_histograms_or_changed_bucket_shapes_do_not_produce_latency():
    before, after = snapshots()
    after["instances"]["pod-a:boot-1"]["stats"]["buckets_ms"] = [20, 200]
    assert "gateway" not in build(before, after).services
