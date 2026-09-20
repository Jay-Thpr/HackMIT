from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from faultline_contracts import Fingerprint, ResourceStats
from faultline_contracts.metrics import RESOURCE_FIELDS, is_valid_metric_key


def _fp(**kw):
    now = datetime.now(timezone.utc)
    return Fingerprint(window_start=now, window_end=now, **kw)


def test_defaults_and_bounds():
    stats = ResourceStats(lag_messages=3, cache_hit_ratio=0.5)
    assert stats.lag_messages == 3
    for bad in ({"lag_messages": -1}, {"cache_hit_ratio": 1.5}, {"cpu_throttled_ratio": -0.1},
                {"outstanding": float("nan")}, {"completed_total": float("inf")},
                {"unknown_field": 1}):
        with pytest.raises(ValidationError):
            ResourceStats(**bad)


def test_metrics_keys():
    fp = _fp(resources={"kafka_partition_0": ResourceStats(lag_messages=4, ready_replicas=3)})
    metrics = fp.metrics()
    assert metrics["resource.kafka_partition_0.lag_messages"] == 4.0
    assert is_valid_metric_key("resource.kafka_partition_0.lag_messages")
    assert is_valid_metric_key("resource.worker_1.cpu_throttled_ratio")
    assert not is_valid_metric_key("resource.x.bogus_field")
    for field in RESOURCE_FIELDS:
        assert is_valid_metric_key(f"resource.shard_0_replica.{field}")


def test_empty_resources_round_trip():
    fp = _fp()
    assert fp.resources == {}
    assert Fingerprint(**fp.model_dump(mode="json")) == fp
