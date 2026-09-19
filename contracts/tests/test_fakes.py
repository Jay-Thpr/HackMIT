from datetime import timedelta
from statistics import mean

import pytest

from faultline_contracts import (
    ActionStatus,
    Fingerprint,
    LeverAdapter,
    LeverError,
    TelemetrySource,
    is_valid_metric_key,
)
from faultline_contracts.fakes import FakeLeverAdapter, FakeWorld, ManualClock, ReplayTelemetrySource
from faultline_contracts.fault import CpuStarveFault, DegradeDbFault, FaultController, StormFault, World

HEALTHY_DB_MS = 200  # healthy query p50 is ~100 ms
SICK_DB_MS = 800


def avg(world: FakeWorld, key: str, seconds: int) -> float:
    fps = world.series(world.now - timedelta(seconds=seconds), world.now)
    return mean(fp.metrics()[key] for fp in fps)


def storm_world(seed=0) -> FakeWorld:
    w = FakeWorld(seed=seed)
    w.advance(60)
    w.storm(StormFault(duration_s=20))
    w.advance(60)
    return w


def degraded_world(seed=0) -> FakeWorld:
    w = FakeWorld(seed=seed)
    w.advance(60)
    w.degrade_db(DegradeDbFault())
    w.advance(60)
    return w


def test_healthy_baseline():
    w = FakeWorld()
    w.advance(60)
    assert avg(w, "db.query_p50_ms", 30) < HEALTHY_DB_MS
    assert avg(w, "svc.gateway.error_rate", 30) < 0.01
    assert not w.latest().slos[0].breached


def test_storm_persists_after_trigger_ends():
    w = FakeWorld(seed=3)
    w.advance(60)
    w.storm(StormFault(duration_s=20))
    w.advance(10)
    assert w.state().active is True
    w.advance(11)
    assert w.state().active is False and w.state().world == World.storm
    w.advance(70)  # >= 60 s after the trigger ended
    for fp in w.series(w.now - timedelta(seconds=60), w.now):
        assert fp.metrics()["db.query_p50_ms"] > SICK_DB_MS
        assert fp.slos[0].breached


def test_storm_and_degraded_look_the_same():
    a, b = storm_world(seed=11), degraded_world(seed=22)
    for key in ("db.qps", "db.query_p50_ms", "svc.orders.retry_ratio", "svc.gateway.error_rate", "db.pool_busy_ratio"):
        va, vb = avg(a, key, 30), avg(b, key, 30)
        assert abs(va - vb) / max(va, vb) < 0.15, (key, va, vb)


def test_retry_cap_breaks_storm_permanently():
    w = storm_world()
    h = w.apply("retry_cap", {"max_retries": 0}, ttl_s=60)
    w.advance(20)
    assert avg(w, "db.query_p50_ms", 10) < HEALTHY_DB_MS
    w.undo(h)
    w.advance(60)
    assert avg(w, "db.query_p50_ms", 60) < HEALTHY_DB_MS
    assert avg(w, "svc.gateway.error_rate", 60) < 0.05


def test_retry_cap_does_not_fix_degraded_db():
    w = degraded_world()
    h = w.apply("retry_cap", {"max_retries": 0}, ttl_s=60)
    w.advance(20)
    assert avg(w, "db.query_p50_ms", 10) > SICK_DB_MS
    assert avg(w, "db.qps", 10) < 120  # load does drop to ~80
    w.undo(h)
    w.advance(30)
    assert avg(w, "db.query_p50_ms", 15) > SICK_DB_MS
    assert avg(w, "db.qps", 15) > 250  # storm returns


def test_db_failover_heals_degraded_db_while_applied():
    w = degraded_world()
    h = w.apply("db_failover", {}, ttl_s=120)
    w.advance(30)
    assert avg(w, "db.query_p50_ms", 10) < HEALTHY_DB_MS
    assert avg(w, "svc.gateway.error_rate", 10) < 0.05
    w.undo(h)
    w.advance(30)
    assert avg(w, "db.query_p50_ms", 15) > SICK_DB_MS


def test_cpu_starve_fits_neither():
    w = FakeWorld(seed=5)
    w.advance(60)
    w.cpu_starve(CpuStarveFault())
    w.advance(60)
    h = w.apply("retry_cap", {"max_retries": 0}, ttl_s=60)
    w.advance(20)
    assert avg(w, "db.query_p50_ms", 10) < HEALTHY_DB_MS  # DB relieved
    assert avg(w, "svc.gateway.error_rate", 10) > 0.5  # checkout still bad
    w.undo(h)
    w.advance(30)
    assert avg(w, "db.qps", 15) > 250  # cascade returns
    h = w.apply("db_failover", {}, ttl_s=120)
    w.advance(30)
    assert avg(w, "svc.gateway.error_rate", 10) > 0.5  # failover does nothing for checkout


def test_ttl_expiry_auto_reverts():
    w = storm_world()
    h = w.apply("retry_cap", {"max_retries": 0}, ttl_s=10)
    w.advance(9)
    assert w.status(h) == ActionStatus.active
    w.advance(1)
    assert w.status(h) == ActionStatus.expired
    assert w.undo(h).status == ActionStatus.expired  # undo after expiry is a no-op

    d = degraded_world()
    h = d.apply("retry_cap", {"max_retries": 0}, ttl_s=15)
    d.advance(45)
    assert d.status(h) == ActionStatus.expired
    assert avg(d, "db.qps", 15) > 250  # cap no longer in effect


def test_undo_status():
    w = FakeWorld()
    h = w.apply("shed", {"fraction": 0.5}, ttl_s=30)
    assert h.applied_at == w.now and h.undo.lever_id == "shed"
    w.advance(5)
    assert w.undo(h).status == ActionStatus.undone
    assert w.status(h) == ActionStatus.undone


@pytest.mark.parametrize("adapter", [FakeWorld(), FakeLeverAdapter()])
@pytest.mark.parametrize(
    "lever_id,params,ttl_s",
    [
        ("nope", {}, 10),
        ("retry_cap", {}, 10),
        ("retry_cap", {"max_retries": 5}, 10),
        ("retry_cap", {"max_retries": 1.5}, 10),
        ("retry_cap", {"max_retries": 0, "extra": 1}, 10),
        ("shed", {"fraction": 2}, 10),
        ("retry_cap", {"max_retries": 0}, 301),
        ("retry_cap", {"max_retries": 0}, 0),
    ],
)
def test_lever_errors(adapter, lever_id, params, ttl_s):
    with pytest.raises(LeverError):
        adapter.apply(lever_id, params, ttl_s)


def test_protocols():
    w = FakeWorld()
    assert isinstance(w, TelemetrySource)
    assert isinstance(w, LeverAdapter)
    assert isinstance(w, FaultController)
    assert isinstance(FakeLeverAdapter(), LeverAdapter)
    assert isinstance(ReplayTelemetrySource([]), TelemetrySource)


def test_blast_radius_and_catalog():
    w = FakeWorld()
    assert {s.id for s in w.catalog()} == {"retry_cap", "shed", "db_failover", "canary_weight"}
    assert w.estimate_blast_radius("retry_cap", {"max_retries": 0}) == 0.0
    assert w.estimate_blast_radius("shed", {"fraction": 0.5}) == 50.0


def test_future_is_off_limits():
    w = FakeWorld()
    w.advance(10)
    with pytest.raises(ValueError):
        w.window(w.now, w.now + timedelta(seconds=5))
    with pytest.raises(ValueError):
        w.series(w.start, w.now + timedelta(seconds=1))
    assert len(w.series(w.start, w.now)) == 2


def test_fingerprint_shape_and_no_leaks():
    for w in (storm_world(), degraded_world()):
        fp = w.latest()
        assert set(fp.services) == {"gateway", "orders", "payments", "fraud_check"}
        assert fp.db is not None and fp.slos[0].metric == "svc.gateway.p99_ms"
        assert any(lh.message == "timeout calling payments" for lh in fp.log_highlights)
        m = fp.metrics()
        assert all(is_valid_metric_key(k) for k in m), [k for k in m if not is_valid_metric_key(k)]
        assert "slo.checkout.value" in m and "edge.payments.db.qps" in m
        js = fp.model_dump_json().lower()
        for word in ("world", "fault", "storm", "degraded", "batch"):
            assert word not in js


def test_determinism():
    a, b = storm_world(seed=7), storm_world(seed=7)
    assert a.latest().model_dump_json() == b.latest().model_dump_json()


def test_canary_v2_does_not_amplify():
    w = storm_world()
    h = w.apply("canary_weight", {"v2_weight": 1.0}, ttl_s=300)
    w.advance(30)
    assert avg(w, "db.qps", 10) < 150
    w.undo(h)


def test_fake_lever_adapter_records_and_expires():
    clock = ManualClock()
    a = FakeLeverAdapter(clock=clock)
    h1 = a.apply("retry_cap", {"max_retries": 0}, ttl_s=20)
    h2 = a.apply("shed", {"fraction": 0.1}, ttl_s=100)
    assert h1.applied_at == clock.now and len(a.applied) == 2
    clock.advance(20)
    assert a.status(h1) == ActionStatus.expired
    assert a.undo(h2).status == ActionStatus.undone
    assert [h.action_id for h in a.undone] == [h2.action_id]
    assert [h.action_id for h in a.expired] == [h1.action_id]
    assert a.active() == []


def test_replay_source_roundtrip(tmp_path):
    w = storm_world()
    fps = w.series(w.start, w.now)
    path = tmp_path / "fps.json"
    path.write_text("[" + ",".join(f.model_dump_json() for f in fps) + "]")
    r = ReplayTelemetrySource.from_json(path)
    assert r.window(fps[3].window_start, fps[3].window_end) == fps[3]
    assert len(r.series(r.start, r.end)) == len(fps)
    merged = r.window(fps[0].window_start, fps[1].window_end)
    assert isinstance(merged, Fingerprint)
    assert merged.metrics()["db.qps"] == pytest.approx((fps[0].db.qps + fps[1].db.qps) / 2)
    with pytest.raises(ValueError):
        r.window(r.end, r.end + timedelta(seconds=5))
