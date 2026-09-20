"""Generate deterministic JSON fixtures into contracts/fixtures/ from the pydantic models.

Run: uv run python scripts/make_fixtures.py
"""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from faultline_contracts import (
    CATALOG,
    LAB_CATALOG,
    Actor,
    AuditEvent,
    CloneEndpoints,
    CloneInfo,
    CloneSpec,
    CloneStatus,
    LabActionHandle,
    ConfirmExpect,
    Confirmation,
    DbStats,
    Direction,
    Edge,
    EventKind,
    Experiment,
    Fingerprint,
    Hypothesis,
    HypothesisSupport,
    LogHighlight,
    MetricExpectation,
    Observation,
    Phase,
    Prediction,
    ServiceStats,
    SloStatus,
    Stage,
    TriageResult,
    Verdict,
    WINDOW_S,
    standard_blast_radius,
)
from faultline_contracts.fault import FaultState, World

OUT = Path(__file__).resolve().parent.parent / "fixtures"
T0 = datetime(2026, 9, 19, 15, 0, 0, tzinfo=timezone.utc)
T_FAULT = T0 + timedelta(seconds=60)
T_CAP = T_FAULT + timedelta(seconds=60)
T_RELEASE = T_CAP + timedelta(seconds=20)
T_END = T_RELEASE + timedelta(seconds=30)
INCIDENT_ID = "inc-20260919-150100"
SLO_THRESHOLD_MS = 1000.0

# Regime = the underlying (noise-free) values for one 5 s window.
HEALTHY = dict(
    rps=80, gw_p50=35, gw_p99=120, gw_err=0.002,
    retry=1.0, timeout=0.0, orders_p99=110, orders_err=0.002,
    pay_qps=80, pay_p50=22, pay_p99=60, pay_err=0.001,
    fc_qps=80, fc_p99=12,
    db_qps=80, q_p50=15, q_p99=45, pool=0.35,
    logs=[],
)
INCIDENT = dict(
    rps=80, gw_p50=1900, gw_p99=2050, gw_err=0.86,
    retry=3.9, timeout=0.88, orders_p99=2000, orders_err=0.86,
    pay_qps=312, pay_p50=640, pay_p99=1750, pay_err=0.55,
    fc_qps=312, fc_p99=13,
    db_qps=318, q_p50=620, q_p99=1700, pool=1.0,
    logs=[
        ("orders", "WARN", "payments call timed out after 500ms, retrying (attempt <n> of 4)", 1150),
        ("orders", "ERROR", "checkout failed: payments unavailable after 4 attempts", 330),
        ("payments", "WARN", "db connection pool exhausted, waited <t>ms for a connection", 890),
        ("payments", "WARN", "slow query: SELECT ... FROM accounts took <t>ms", 1270),
    ],
)
# Retry cap 0 while the loop is self-sustaining: load collapses to 80/s, DB recovers.
CAP_RECOVERS = dict(
    HEALTHY,
    gw_p50=40, gw_p99=160, gw_err=0.01, orders_err=0.01, pay_p99=75, q_p50=17, q_p99=55, pool=0.4,
    logs=[("orders", "INFO", "retry override active: max_retries=0", 16)],
)
# Retry cap 0 while DB capacity is ~40/s: load drops to 80/s but queries stay slow.
CAP_STILL_SLOW = dict(
    INCIDENT,
    gw_p50=520, gw_p99=560, gw_err=0.5, retry=1.0, timeout=0.5, orders_p99=510, orders_err=0.5,
    pay_qps=80, pay_p50=560, pay_p99=1400, pay_err=0.3, fc_qps=80,
    db_qps=81, q_p50=580, q_p99=1500, pool=1.0,
    logs=[
        ("orders", "INFO", "retry override active: max_retries=0", 16),
        ("orders", "ERROR", "checkout failed: payments call timed out after 500ms", 200),
        ("payments", "WARN", "slow query: SELECT ... FROM accounts took <t>ms", 390),
    ],
)


def _n(rng: random.Random, v: float, rel: float = 0.04) -> float:
    return v * (1 + rng.gauss(0, rel))


def fingerprint(start: datetime, r: dict, rng: random.Random) -> Fingerprint:
    def n(v: float, nd: int = 1, cap: float | None = None) -> float:
        x = max(0.0, _n(rng, v))
        if cap is not None:
            x = min(cap, x)
        return round(x, nd)

    gw_p99 = n(r["gw_p99"])
    retry = 1.0 if r["retry"] == 1.0 else n(r["retry"], 2)
    return Fingerprint(
        window_start=start,
        window_end=start + timedelta(seconds=WINDOW_S),
        services={
            "gateway": ServiceStats(qps=n(r["rps"]), p50_ms=n(r["gw_p50"]), p99_ms=gw_p99,
                                    error_rate=n(r["gw_err"], 4, 1.0)),
            "orders": ServiceStats(qps=n(r["rps"]), p99_ms=n(r["orders_p99"]), error_rate=n(r["orders_err"], 4, 1.0),
                                   retry_ratio=retry, timeout_rate=n(r["timeout"], 4, 1.0)),
            "payments": ServiceStats(qps=n(r["pay_qps"]), p50_ms=n(r["pay_p50"]), p99_ms=n(r["pay_p99"]),
                                     error_rate=n(r["pay_err"], 4, 1.0)),
            "fraud_check": ServiceStats(qps=n(r["fc_qps"]), p99_ms=n(r["fc_p99"]), error_rate=0.0),
        },
        db=DbStats(qps=n(r["db_qps"]), query_p50_ms=n(r["q_p50"]), query_p99_ms=n(r["q_p99"]),
                   pool_busy_ratio=n(r["pool"], 3, 1.0)),
        edges=[
            Edge(src="gateway", dst="orders", qps=n(r["rps"]), p99_ms=n(r["orders_p99"]),
                 error_rate=n(r["orders_err"], 4, 1.0)),
            Edge(src="orders", dst="payments", qps=n(r["pay_qps"]), p99_ms=n(r["pay_p99"]),
                 error_rate=n(r["pay_err"], 4, 1.0)),
            Edge(src="payments", dst="db", qps=n(r["db_qps"]), p99_ms=n(r["q_p99"])),
            Edge(src="payments", dst="fraud_check", qps=n(r["fc_qps"]), p99_ms=n(r["fc_p99"]), error_rate=0.0),
        ],
        slos=[SloStatus(name="checkout", metric="svc.gateway.p99_ms", threshold=SLO_THRESHOLD_MS,
                        value=gw_p99, breached=gw_p99 > SLO_THRESHOLD_MS)],
        log_highlights=[LogHighlight(service=s, level=lv, message=m, count=int(_n(rng, c)))
                        for s, lv, m, c in r["logs"]],
    )


def series(seed: int, during_cap: dict, after_release: dict) -> list[Fingerprint]:
    rng = random.Random(seed)
    phases = [(T0, T_FAULT, HEALTHY), (T_FAULT, T_CAP, INCIDENT), (T_CAP, T_RELEASE, during_cap),
              (T_RELEASE, T_END, after_release)]
    out = []
    for a, b, regime in phases:
        t = a
        while t < b:
            out.append(fingerprint(t, regime, rng))
            t += timedelta(seconds=WINDOW_S)
    return out


def at(fps: list[Fingerprint], t: datetime) -> Fingerprint:
    return next(f for f in fps if f.window_start == t)


def triage_hero() -> TriageResult:
    def me(metric: str, d: Direction) -> MetricExpectation:
        return MetricExpectation(metric=metric, direction=d)

    up, down, flat = Direction.up, Direction.down, Direction.flat
    return TriageResult(
        incident_id=INCIDENT_ID,
        created_at=T_FAULT + timedelta(seconds=40),
        ambiguous=True,
        reasoning=(
            "DB receives ~4x the checkout rate (retry_ratio ~4) with query p50 above the 500ms client timeout "
            "and the pool saturated. This is consistent both with a retry loop that keeps a healthy DB overloaded "
            "and with a DB whose capacity dropped below 80 queries/s. Telemetry alone cannot separate them."
        ),
        hypotheses=[
            Hypothesis(
                id="H_meta", label="Self-sustaining retry loop",
                description="A transient DB slowdown pushed queries past the 500ms timeout; Orders' retries (4 "
                            "attempts) now keep DB load at ~320/s, which sustains the slowness after the trigger ended.",
                evidence=["db.qps ~320 vs 80 checkouts/s", "svc.orders.retry_ratio ~3.9",
                          "db.query_p50_ms ~620 > 500ms client timeout"],
            ),
            Hypothesis(
                id="H_db", label="DB capacity reduced",
                description="Something outside the app (e.g. a batch job) cut the DB's capacity below the "
                            "80/s demand; retries amplify load but are a symptom, not the cause.",
                evidence=["db.pool_busy_ratio 1.0", "db.query_p99_ms ~1700",
                          "no deploy or config change events in the window"],
            ),
        ],
        predictions=[
            Prediction(
                hypothesis_id="H_meta", experiment_id="retry_cap_0_20s",
                during=[me("db.query_p50_ms", down), me("db.qps", down), me("svc.gateway.error_rate", down)],
                after_release=[me("db.query_p50_ms", flat), me("svc.orders.retry_ratio", flat)],
                confirms_if=Confirmation(phase=Phase.after_release, metric="db.query_p50_ms",
                                         expect=ConfirmExpect.within_baseline),
            ),
            Prediction(
                hypothesis_id="H_db", experiment_id="retry_cap_0_20s",
                during=[me("db.query_p50_ms", flat), me("db.qps", down), me("svc.gateway.error_rate", flat)],
                after_release=[me("db.query_p50_ms", flat), me("svc.orders.retry_ratio", up)],
                # Retry capping separates the worlds but does not directly repair
                # capacity. H_db can only be confirmed by the failover prediction
                # below; host contention can also stay slow under a retry cap.
                confirms_if=None,
            ),
            Prediction(
                hypothesis_id="H_meta", experiment_id="db_failover_30s",
                during=[me("db.query_p50_ms", flat), me("svc.orders.retry_ratio", flat)],
                after_release=[me("db.query_p50_ms", flat)],
                # A failover no-op is useful separation evidence, but it is not
                # a positive recovery test for a retry loop.
                confirms_if=None,
            ),
            Prediction(
                hypothesis_id="H_db", experiment_id="db_failover_30s",
                during=[me("db.query_p50_ms", down), me("svc.orders.retry_ratio", down),
                        me("svc.gateway.error_rate", down), me("svc.gateway.p99_ms", down)],
                after_release=[me("db.query_p50_ms", up)],
                # A degraded dependency is confirmed only if relieving it heals the user-facing
                # SLO. DB latency alone also drops when the DB is merely a victim (e.g. a
                # CPU-starved caller), which must stay none-of-the-above.
                confirms_if=Confirmation(phase=Phase.during, metric="svc.gateway.p99_ms", expect=ConfirmExpect.down),
            ),
        ],
    )


def experiments() -> list[Experiment]:
    specs = [
        ("retry_cap_0_20s", "retry_cap", {"max_retries": 0}, 20),
        ("shed_10_20s", "shed", {"fraction": 0.1}, 20),
        ("shed_50_20s", "shed", {"fraction": 0.5}, 20),
        ("db_failover_30s", "db_failover", {}, 30),
    ]
    return [Experiment(id=i, lever_id=lv, params=p, hold_s=h, blast_radius_pct=standard_blast_radius(lv, p))
            for i, lv, p, h in specs]


def verdict_storm() -> Verdict:
    def obs(metric: str, phase: Phase, baseline: float, measured: float, sigma: float) -> Observation:
        z = round((measured - baseline) / sigma, 2)
        d = Direction.flat if abs(z) < 3 else (Direction.up if z > 0 else Direction.down)
        return Observation(experiment_id="retry_cap_0_20s", metric=metric, phase=phase, baseline=baseline,
                           measured=measured, sigma=sigma, z=z, direction=d)

    # during: compared with the incident steady state; after_release: compared with the healthy baseline.
    return Verdict(
        incident_id=INCIDENT_ID,
        created_at=T_END,
        diagnosis="H_meta",
        confirmed=True,
        support=[HypothesisSupport(hypothesis_id="H_meta", support=0.93, confirmed=True),
                 HypothesisSupport(hypothesis_id="H_db", support=0.07, confirmed=None)],
        observations=[
            obs("db.query_p50_ms", Phase.during, 620.0, 17.2, 62.0),
            obs("db.qps", Phase.during, 318.0, 80.4, 31.8),
            obs("svc.gateway.error_rate", Phase.during, 0.86, 0.011, 0.086),
            obs("db.query_p50_ms", Phase.after_release, 15.0, 15.6, 1.5),
            obs("svc.orders.retry_ratio", Phase.after_release, 1.0, 1.0, 0.1),
        ],
        summary="Capping retries collapsed DB query time to baseline and it stayed there after release: "
                "the loop was self-sustaining (H_meta). The cap was the fix; confirmation passed.",
    )


def audit_hero() -> list[AuditEvent]:
    def ev(n: int, sec: float, stage: Stage, kind: EventKind, actor: Actor, summary: str, payload: dict,
           action_id: str | None = None, experiment_id: str | None = None) -> AuditEvent:
        return AuditEvent(event_id=f"evt-{n:03d}", incident_id=INCIDENT_ID, ts=T_FAULT + timedelta(seconds=sec),
                          stage=stage, kind=kind, actor=actor, summary=summary, payload=payload,
                          action_id=action_id, experiment_id=experiment_id)

    exp, act = "retry_cap_0_20s", "act-001"
    cap_s = (T_CAP - T_FAULT).total_seconds()
    rel_s = (T_RELEASE - T_FAULT).total_seconds()
    return [
        ev(1, 15, Stage.detect, EventKind.detect, Actor.math, "SLO checkout breached: gateway p99 2050ms > 1000ms",
           {"slo": "checkout", "metric": "svc.gateway.p99_ms", "value": 2050.0, "threshold": 1000.0}),
        ev(2, 40, Stage.triage, EventKind.triage, Actor.llm, "2 hypotheses (H_meta, H_db); ambiguous",
           {"hypotheses": ["H_meta", "H_db"], "ambiguous": True}),
        ev(3, cap_s, Stage.experiment, EventKind.experiment_start, Actor.orchestrator,
           "Experiment retry_cap_0_20s started (blast radius 0%)",
           {"lever_id": "retry_cap", "params": {"max_retries": 0}, "hold_s": 20, "blast_radius_pct": 0.0},
           action_id=act, experiment_id=exp),
        ev(4, cap_s, Stage.experiment, EventKind.action_apply, Actor.adapter, "retry_cap max_retries=0 applied, ttl 60s",
           {"lever_id": "retry_cap", "params": {"max_retries": 0}, "ttl_s": 60}, action_id=act, experiment_id=exp),
        ev(5, rel_s, Stage.experiment, EventKind.action_undo, Actor.adapter, "retry_cap override removed",
           {"lever_id": "retry_cap"}, action_id=act, experiment_id=exp),
        ev(6, rel_s, Stage.experiment, EventKind.experiment_end, Actor.orchestrator,
           "Experiment retry_cap_0_20s released; watching 30s", {"watch_s": 30}, action_id=act, experiment_id=exp),
        ev(7, 110, Stage.experiment, EventKind.verdict, Actor.math,
           "H_meta confirmed: DB query p50 stayed at baseline after release",
           {"diagnosis": "H_meta", "confirmed": True, "support": {"H_meta": 0.93, "H_db": 0.07}}, experiment_id=exp),
        ev(8, 112, Stage.mitigate, EventKind.mitigation, Actor.orchestrator,
           "No mitigation lever needed: retry cap broke the loop; requesting code fix",
           {"mitigation": "none_needed"}),
        ev(9, 180, Stage.patch, EventKind.patch_opened, Actor.orchestrator,
           "Devin PR opened: exponential backoff + jitter and retry budget in orders",
           {"pr_url": "https://github.com/example/sandbox/pull/1", "branch": "fix/orders-backoff"}),
        ev(10, 420, Stage.canary, EventKind.canary_update, Actor.adapter, "orders-v2 canary weight 0.05",
           {"lever_id": "canary_weight", "params": {"v2_weight": 0.05}, "ttl_s": 1800}, action_id="act-002"),
        ev(11, 600, Stage.report, EventKind.report, Actor.llm, "Incident report written",
           {"report_path": "reports/inc-20260919-150100.md"}),
    ]


def clone_hero() -> CloneInfo:
    """Investigator A's clone (H_meta) right after it reproduced the incident with a transient DB slowdown."""
    created = T_FAULT + timedelta(seconds=45)
    return CloneInfo(
        clone_id="clone-1",
        status=CloneStatus.ready,
        spec=CloneSpec(name="h_meta"),
        created_at=created,
        endpoints=CloneEndpoints(
            gateway_url="http://localhost:18080",
            control_url="http://localhost:19901",
            stats_urls={"orders": "http://localhost:18101", "payments": "http://localhost:18102",
                        "loadgen": "http://localhost:18103"},
        ),
        active_actions=[LabActionHandle(action_id="lab-001", clone_id="clone-1", action="db_latency",
                                        params={"extra_ms": 800}, applied_at=created + timedelta(seconds=30),
                                        ttl_s=20)],
    )


def dump(name: str, obj) -> None:
    path = OUT / name
    if isinstance(obj, list):
        data = [o.model_dump(mode="json") if hasattr(o, "model_dump") else o for o in obj]
    else:
        data = obj.model_dump(mode="json")
    path.write_text(json.dumps(data, indent=2) + "\n")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    storm = series(1, CAP_RECOVERS, HEALTHY)
    degraded = series(2, CAP_STILL_SLOW, INCIDENT)
    steady = T_FAULT + timedelta(seconds=30)
    dump("fingerprint_healthy.json", at(storm, T0 + timedelta(seconds=30)))
    dump("fingerprint_storm.json", at(storm, steady))
    dump("fingerprint_degraded_db.json", at(degraded, steady))
    dump("series_storm_experiment.json", storm)
    dump("series_degraded_db_experiment.json", degraded)
    dump("triage_hero.json", triage_hero())
    dump("catalog.json", CATALOG)
    dump("experiments.json", experiments())
    dump("verdict_storm.json", verdict_storm())
    (OUT / "audit_hero.jsonl").write_text("".join(e.model_dump_json() + "\n" for e in audit_hero()))
    dump("lab_catalog.json", LAB_CATALOG)
    dump("clone_hero.json", clone_hero())
    dump("fault_state_storm.json", FaultState(world=World.storm, active=False,
                                              params={"delay_ms": 800, "duration_s": 20}, started_at=T_FAULT))
    print(f"wrote fixtures to {OUT}")


if __name__ == "__main__":
    main()
