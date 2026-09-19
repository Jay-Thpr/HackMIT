"""FakeWorld: a seeded discrete-time simulator of the hero sandbox.

Topology: gateway -> orders -> payments -> db, payments -> fraud_check.

One object implements TelemetrySource, LeverAdapter and FaultController, so a single
FakeWorld drives the whole pipeline (and the benchmark) without Docker.

Clock: simulated, starts at `start`. `advance(seconds)` simulates forward in 1 s ticks;
window()/series() only read recorded history and raise ValueError for the future.

Model (per 1 s tick):
  offered DB load  = admitted_rps * attempts_per_request
  attempts         = sum_{k=0..R} p^k,  p = P(payments call > 500 ms timeout), lognormal latency
  db latency       -> base / (1 - load/capacity), clipped at DB_SAT_MS once saturated
                      (queries wait for a pool slot / statement timeout), smoothed over ticks.
The feedback (latency -> timeouts -> retries -> load -> latency) is what makes the storm
self-sustaining at capacity 100 once load reaches ~320 q/s. Because latency is clipped at
saturation, capacity 100 (storm) and capacity 40 (degraded_db) look the same at steady state.
"""

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..common import WINDOW_S
from ..fault import CpuStarveFault, DegradeDbFault, FaultState, StormFault, World
from ..fingerprint import DbStats, Edge, Fingerprint, LogHighlight, ServiceStats, SloStatus
from ..levers import CATALOG, ActionHandle, ActionStatus, LeverError, LeverSpec, UndoSpec, standard_blast_radius
from .fake_levers import validate_lever_call
from .fake_telemetry import merge_fingerprints

# ---- sandbox constants -----------------------------------------------------------------
LOAD_RPS = 80.0  # checkouts/s
DB_CAPACITY = 100.0  # healthy queries/s
STANDBY_CAPACITY = 400.0  # db_failover target (fresh standby, no batch job)
TIMEOUT_MS = 500.0  # orders -> payments timeout
MAX_RETRIES = 3  # default: 4 attempts
DB_BASE_MS = 20.0  # unloaded query time
DB_SAT_MS = 1400.0  # query time once saturated (pool queue / statement timeout)
PAYMENTS_BASE_MS = 20.0
FRAUD_MS = 15.0
LAT_SIGMA = 0.3  # lognormal spread of a single call's latency
NOISE = 0.08  # per-tick multiplicative noise (~3.5% per 5 s window)
SLO_THRESHOLD_MS = 1000.0
SMOOTH = 0.5  # fraction of the gap to target latency closed per tick

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _p_over(median_ms: float, threshold_ms: float) -> float:
    """P(lognormal(median, LAT_SIGMA) > threshold)."""
    z = math.log(threshold_ms / max(median_ms, 1e-6)) / LAT_SIGMA
    return 0.5 * math.erfc(z / math.sqrt(2))


class FakeWorld:
    def __init__(self, seed: int = 0, start: datetime = EPOCH, load_rps: float = LOAD_RPS):
        self.rng = random.Random(seed)
        self.start = start
        self.load_rps = load_rps
        self._t = 0  # simulated seconds since start
        self._ticks: list[Fingerprint] = []  # one 1 s fingerprint per simulated second
        # hidden fault state
        self._world = World.none
        self._fault_params: dict[str, Any] = {}
        self._fault_started: datetime | None = None
        self._storm_until: datetime | None = None
        self._storm_delay_ms = 0.0
        self._db_capacity = DB_CAPACITY
        self._payments_extra_ms = 0.0
        # dynamic state
        self._db_ms = DB_BASE_MS / (1 - load_rps / DB_CAPACITY)
        # levers
        self._actions: dict[str, ActionHandle] = {}

    # ---- clock -------------------------------------------------------------------------
    @property
    def now(self) -> datetime:
        return self.start + timedelta(seconds=self._t)

    def advance(self, seconds: float) -> datetime:
        """Simulate forward `seconds` (rounded to whole seconds). Returns the new now."""
        for _ in range(int(round(seconds))):
            self._expire_actions()
            self._ticks.append(self._tick())
            self._t += 1
        return self.now

    def latest(self, seconds: int = WINDOW_S) -> Fingerprint:
        """Fingerprint of the last `seconds` of simulated time."""
        return self.window(self.now - timedelta(seconds=seconds), self.now)

    # ---- simulation --------------------------------------------------------------------
    def _levers(self) -> dict[str, Any]:
        eff = {"max_retries": MAX_RETRIES, "shed": 0.0, "failover": False, "v2_weight": 0.0}
        for h in self._actions.values():  # insertion order: later actions win
            if h.status != ActionStatus.active:
                continue
            if h.lever_id == "retry_cap":
                eff["max_retries"] = min(MAX_RETRIES, h.params["max_retries"])
            elif h.lever_id == "shed":
                eff["shed"] = float(h.params["fraction"])
            elif h.lever_id == "db_failover":
                eff["failover"] = True
            elif h.lever_id == "canary_weight":
                eff["v2_weight"] = float(h.params["v2_weight"])
        return eff

    def _noise(self, x: float) -> float:
        return x * max(0.0, 1.0 + self.rng.gauss(0.0, NOISE))

    def _tick(self) -> Fingerprint:
        now = self.now
        lv = self._levers()
        r = lv["max_retries"]
        w = lv["v2_weight"]

        # timeouts are driven by the current DB latency (feedback loop)
        storm_delay = self._storm_delay_ms if (self._storm_until and now < self._storm_until) else 0.0
        pay_ms = PAYMENTS_BASE_MS + self._payments_extra_ms + FRAUD_MS + self._db_ms
        p = min(_p_over(pay_ms, TIMEOUT_MS), 0.9999)

        # attempts per logical request: v1 retries immediately, v2 backs off (≈ one budgeted retry)
        attempts_v1 = sum(p**k for k in range(r + 1))
        attempts_v2 = 1.0 + 0.25 * p
        attempts = (1 - w) * attempts_v1 + w * attempts_v2
        fail_v1 = p ** (r + 1)
        fail_v2 = p * (1 - 0.25 * (1 - p))
        fail = (1 - w) * fail_v1 + w * fail_v2

        shed = lv["shed"]
        admitted = self.load_rps * (1 - shed)
        calls = admitted * attempts  # orders -> payments, payments -> db, payments -> fraud

        capacity = STANDBY_CAPACITY if lv["failover"] else self._db_capacity
        rho = calls / capacity
        target = min(DB_BASE_MS / max(1 - rho, 1e-3), DB_SAT_MS) + storm_delay
        self._db_ms += SMOOTH * (target - self._db_ms)

        # ---- derived telemetry (noisy) ----
        pay_p99 = min(pay_ms * math.exp(2.33 * LAT_SIGMA), 5000.0)
        per_attempt = p * TIMEOUT_MS + (1 - p) * min(pay_ms, TIMEOUT_MS)
        orders_p50 = attempts * per_attempt + 3.0
        tail_attempts = max((j for j in range(r + 1) if p**j >= 0.01), default=0)
        orders_p99 = tail_attempts * TIMEOUT_MS + min(pay_p99, TIMEOUT_MS) + 5.0
        gw_err = shed + (1 - shed) * fail
        busy = min(1.0, rho)

        n = self._noise
        clamp = lambda x: min(1.0, max(0.0, x))  # noqa: E731
        err = clamp(n(gw_err + 0.001))
        timeout_rate = clamp(n(p + 0.0005))
        retry_ratio = 1.0 + n(attempts - 1.0 + 0.002)
        gw_p50, gw_p99 = n(orders_p50 + 2.0), n(orders_p99 + 4.0)
        db_q50 = n(self._db_ms)
        db_err = 0.05 if rho >= 1 else 0.001

        services = {
            "gateway": ServiceStats(qps=n(self.load_rps), p50_ms=gw_p50, p99_ms=gw_p99, error_rate=err,
                                    retry_ratio=1.0, timeout_rate=timeout_rate * (1 - shed)),
            "orders": ServiceStats(qps=n(admitted), p50_ms=n(orders_p50), p99_ms=n(orders_p99),
                                   error_rate=clamp(n(fail + 0.001)), retry_ratio=retry_ratio, timeout_rate=timeout_rate),
            "payments": ServiceStats(qps=n(calls), p50_ms=n(pay_ms), p99_ms=n(pay_p99), error_rate=clamp(n(db_err)),
                                     retry_ratio=1.0, timeout_rate=clamp(n(0.001 + 0.02 * busy * (rho >= 1)))),
            "fraud_check": ServiceStats(qps=n(calls), p50_ms=n(FRAUD_MS), p99_ms=n(FRAUD_MS * 2.0),
                                        error_rate=clamp(n(0.001)), retry_ratio=1.0, timeout_rate=0.0),
        }
        db = DbStats(qps=n(calls), query_p50_ms=db_q50, query_p99_ms=n(min(self._db_ms * 2.0, 5000.0)),
                     pool_busy_ratio=clamp(n(busy)))
        edges = [
            Edge(src="gateway", dst="orders", qps=services["orders"].qps, p99_ms=gw_p99, error_rate=err),
            Edge(src="orders", dst="payments", qps=n(calls), p99_ms=n(min(pay_p99, TIMEOUT_MS)), error_rate=timeout_rate),
            Edge(src="payments", dst="fraud_check", qps=n(calls), p99_ms=n(FRAUD_MS * 2.0), error_rate=clamp(n(0.001))),
            Edge(src="payments", dst="db", qps=db.qps, p99_ms=db.query_p99_ms, error_rate=clamp(n(db_err))),
        ]
        slos = [SloStatus(name="checkout", metric="svc.gateway.p99_ms", threshold=SLO_THRESHOLD_MS,
                          value=gw_p99, breached=gw_p99 > SLO_THRESHOLD_MS)]
        logs = []
        n_timeouts = round(calls * p)
        if n_timeouts:
            logs.append(LogHighlight(service="orders", level="WARN", message="timeout calling payments", count=n_timeouts))
        if round(admitted * fail):
            logs.append(LogHighlight(service="gateway", level="ERROR", message="checkout failed: upstream timeout",
                                     count=round(admitted * fail)))
        if rho >= 1:
            logs.append(LogHighlight(service="payments", level="ERROR", message="db connection pool exhausted",
                                     count=round(calls - capacity)))
        return Fingerprint(window_start=now, window_end=now + timedelta(seconds=1), services=services, db=db,
                           edges=edges, slos=slos, log_highlights=logs)

    # ---- TelemetrySource ---------------------------------------------------------------
    def window(self, start: datetime, end: datetime) -> Fingerprint:
        if end > self.now:
            raise ValueError(f"window end {end} is in the future (now={self.now}); call advance() first")
        i0 = max(0, math.ceil((start - self.start).total_seconds()))
        i1 = int((end - self.start).total_seconds())
        ticks = self._ticks[i0:i1]
        if not ticks:
            raise ValueError(f"no simulated data in [{start}, {end})")
        return merge_fingerprints(ticks, start, end)

    def series(self, start: datetime, end: datetime, step_s: int = WINDOW_S) -> list[Fingerprint]:
        if end > self.now:
            raise ValueError(f"series end {end} is in the future (now={self.now}); call advance() first")
        out, t, step = [], start, timedelta(seconds=step_s)
        while t + step <= end:
            out.append(self.window(t, t + step))
            t += step
        return out

    # ---- LeverAdapter ------------------------------------------------------------------
    def catalog(self) -> list[LeverSpec]:
        return list(CATALOG)

    def estimate_blast_radius(self, lever_id: str, params: dict[str, Any]) -> float:
        return standard_blast_radius(lever_id, params)

    def apply(self, lever_id: str, params: dict[str, Any], ttl_s: int) -> ActionHandle:
        validate_lever_call(lever_id, params, ttl_s)
        h = ActionHandle(
            action_id=uuid4().hex,
            lever_id=lever_id,
            params=dict(params),
            applied_at=self.now,
            ttl_s=ttl_s,
            undo=UndoSpec(lever_id=lever_id, payload={"action": "revert"}),
        )
        self._actions[h.action_id] = h
        return h

    def _expire_actions(self) -> None:
        for aid, h in self._actions.items():
            if h.status == ActionStatus.active and self.now >= h.expires_at:
                self._actions[aid] = h.model_copy(update={"status": ActionStatus.expired})

    def undo(self, handle: ActionHandle) -> ActionHandle:
        self._expire_actions()
        h = self._actions.get(handle.action_id)
        if h is None:
            raise LeverError(f"unknown action {handle.action_id!r}")
        if h.status == ActionStatus.active:
            h = h.model_copy(update={"status": ActionStatus.undone})
            self._actions[h.action_id] = h
        return h

    def status(self, handle: ActionHandle) -> ActionStatus:
        self._expire_actions()
        h = self._actions.get(handle.action_id)
        if h is None:
            raise LeverError(f"unknown action {handle.action_id!r}")
        return h.status

    # ---- FaultController (hidden from Faultline) --------------------------------------
    def _set_world(self, world: World, params: dict[str, Any]) -> FaultState:
        self._world, self._fault_params, self._fault_started = world, params, self.now
        return self.state()

    def storm(self, fault: StormFault) -> FaultState:
        self._storm_delay_ms = float(fault.delay_ms)
        self._storm_until = self.now + timedelta(seconds=fault.duration_s)
        return self._set_world(World.storm, fault.model_dump())

    def degrade_db(self, fault: DegradeDbFault) -> FaultState:
        self._db_capacity = fault.capacity_qps
        return self._set_world(World.degraded_db, fault.model_dump())

    def cpu_starve(self, fault: CpuStarveFault) -> FaultState:
        self._payments_extra_ms = 70.0 / max(fault.cpus, 0.01)  # 0.1 cpus -> +700 ms
        return self._set_world(World.cpu_starve, fault.model_dump())

    def reset(self) -> FaultState:
        """Clear every fault and return the system to a healthy state (levers untouched)."""
        self._storm_until, self._storm_delay_ms = None, 0.0
        self._db_capacity, self._payments_extra_ms = DB_CAPACITY, 0.0
        self._db_ms = DB_BASE_MS / (1 - self.load_rps / DB_CAPACITY)
        self._world, self._fault_params, self._fault_started = World.none, {}, None
        return self.state()

    def state(self) -> FaultState:
        if self._world == World.none:
            active = False
        elif self._world == World.storm:
            active = self._storm_until is not None and self.now < self._storm_until
        else:
            active = True
        return FaultState(world=self._world, active=active, params=self._fault_params, started_at=self._fault_started)
