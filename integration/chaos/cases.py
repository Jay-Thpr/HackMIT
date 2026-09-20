"""The chaos catalogue. Each case is declarative: a hidden trigger, an optional schedule of
hidden-world actions fired while Faultline runs, optional wrappers that break what Faultline
sees or controls, and the expected outcome.

`expect_final` is the set of acceptable terminal states (see harness.FINALS). `expect_diagnosis`
is a hypothesis id, a set of acceptable ids, or None when the diagnosis is unconstrained and only
the safety invariants matter. `known_gap` marks an expectation the system currently misses; the
test is xfail(strict=True) so the gap is tracked, not hidden, and a fix flips it to a failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from faultline_contracts import Fingerprint, LeverError, TriageResult
from faultline_contracts.fakes import FakeWorld
from faultline_contracts.fakes.sim import EPOCH, LOAD_RPS
from faultline_contracts.fault import CpuStarveFault, DegradeDbFault, StormFault
from faultline_product.adapters import FixtureLeverAdapter
from faultline_product.ports import CanaryPreparationError, HypothesisInvestigation, PatchProposal


@dataclass(frozen=True)
class ChaosCase:
    id: str
    story: str
    trigger: Callable[[FakeWorld], object] | None
    expect_final: frozenset[str]
    expect_diagnosis: str | frozenset[str] | None = None
    develop_s: int = 60
    healthy_s: int = 120
    load_rps: float = LOAD_RPS
    prepare: Callable[[FakeWorld], None] | None = None  # mutate the hidden world before warm-up
    schedule: tuple[tuple[int, Callable[[FakeWorld], object]], ...] = ()  # (s after run start, action)
    telemetry: Callable[[Any], Any] | None = None  # wrap what Faultline sees (C1)
    levers: Callable[[Any], Any] | None = None  # wrap what Faultline controls (C3)
    brain: Callable[[Any], Any] | None = None  # wrap the Brain (LLM + math)
    patches: Any | None = None
    canary: Any | None = None
    investigation: Any | None = None
    investigation_gate: bool = False
    action_budget: int = 5
    max_revisions: int = 1
    seeds: tuple[int, ...] = (1, 2, 3)
    known_gap: str | None = None

    def diagnosis_ok(self, diagnosis: str | None) -> bool:
        if self.expect_diagnosis is None:
            return True
        if isinstance(self.expect_diagnosis, frozenset):
            return diagnosis in self.expect_diagnosis
        return diagnosis == self.expect_diagnosis

    def expected(self, final: str, diagnosis: str | None) -> bool:
        """Terminal state as expected, and the diagnosis too (unless Faultline never ran)."""
        return final in self.expect_final and (final == NO_BREACH or self.diagnosis_ok(diagnosis))


F = frozenset
READY, ESCALATED, PAGED, REFUSED, RAISED, NO_BREACH = "ready", "escalated", "paged", "refused", "raised", "no_breach"
NONE = "none_of_the_above"


# ---- hidden-world triggers ----------------------------------------------------------------------
def storm(delay_ms: int = 800, duration_s: int = 20):
    def trigger(world: FakeWorld):
        return world.storm(StormFault(delay_ms=delay_ms, duration_s=duration_s))
    trigger.__name__ = f"storm({delay_ms}ms,{duration_s}s)"
    return trigger


def degrade(capacity_qps: float = 40.0):
    def trigger(world: FakeWorld):
        return world.degrade_db(DegradeDbFault(capacity_qps=capacity_qps))
    trigger.__name__ = f"degrade({capacity_qps:g}qps)"
    return trigger


def cpu_starve(cpus: float = 0.1):
    def trigger(world: FakeWorld):
        return world.cpu_starve(CpuStarveFault(service="payments", cpus=cpus))
    trigger.__name__ = f"cpu_starve({cpus:g})"
    return trigger


def both(*triggers):
    def trigger(world: FakeWorld):
        for t in triggers:
            t(world)
    trigger.__name__ = "+".join(t.__name__ for t in triggers)
    return trigger


def heal(world: FakeWorld):
    """The hidden cause disappears on its own (someone killed the batch job)."""
    return world.reset()


def noisy(sigma: float):
    def prepare(world: FakeWorld) -> None:
        world._noise = lambda x: x * max(0.0, 1.0 + world.rng.gauss(0.0, sigma))  # type: ignore[method-assign]
    return prepare


# ---- telemetry chaos (wrap what Faultline sees) ------------------------------------------------
class _Telemetry:
    def __init__(self, source):
        self._source = source

    def window(self, start, end):
        return self._source.window(start, end)

    def series(self, start, end, step_s=5):
        return self._source.series(start, end, step_s)


class DroppedWindows(_Telemetry):
    """The poller missed every n-th 5 s window (scrape timeouts)."""

    def __init__(self, source, every: int = 3):
        super().__init__(source)
        self.every = every

    def series(self, start, end, step_s=5):
        return [fp for i, fp in enumerate(self._source.series(start, end, step_s)) if i % self.every]


class FlakySeries(_Telemetry):
    """The telemetry backend is down for the n-th series() call (e.g. mid-experiment)."""

    def __init__(self, source, fail_on: int = 2):
        super().__init__(source)
        self._calls, self.fail_on = 0, fail_on

    def series(self, start, end, step_s=5):
        self._calls += 1
        if self._calls == self.fail_on:
            raise ConnectionError("telemetry unavailable: connection reset by peer")
        return self._source.series(start, end, step_s)


class Lagging(_Telemetry):
    """Every read returns data `lag_s` older than asked (ingest lag / clock skew)."""

    def __init__(self, source, lag_s: int = 10):
        super().__init__(source)
        self.lag = timedelta(seconds=lag_s)

    def window(self, start, end):
        return self._source.window(start - self.lag, end - self.lag)

    def series(self, start, end, step_s=5):
        return self._source.series(start - self.lag, end - self.lag, step_s)


class NoDbExporter(_Telemetry):
    """The DB exporter is gone: `db` is honestly None (never 0) in every window."""

    @staticmethod
    def _blind(fp: Fingerprint) -> Fingerprint:
        return fp.model_copy(update={"db": None, "edges": [e for e in fp.edges if e.dst != "db"]})

    def window(self, start, end):
        return self._blind(self._source.window(start, end))

    def series(self, start, end, step_s=5):
        return [self._blind(fp) for fp in self._source.series(start, end, step_s)]


class CounterResetAfter(_Telemetry):
    """Violates the contract on purpose: after `at`, the DB exporter reports 0 instead of None
    (a restarted exporter). A judge that trusts zeros will read a fake drop."""

    def __init__(self, source, at: datetime):
        super().__init__(source)
        self.at = at

    def _zero(self, fp: Fingerprint) -> Fingerprint:
        if fp.window_start < self.at or fp.db is None:
            return fp
        return fp.model_copy(update={"db": fp.db.model_copy(update={"qps": 0.0, "query_p50_ms": 0.0, "query_p99_ms": 0.0})})

    def window(self, start, end):
        return self._zero(self._source.window(start, end))

    def series(self, start, end, step_s=5):
        return [self._zero(fp) for fp in self._source.series(start, end, step_s)]


# ---- lever chaos (wrap what Faultline controls) ------------------------------------------------
class _Levers:
    def __init__(self, adapter):
        self._adapter = adapter

    def catalog(self):
        return self._adapter.catalog()

    def estimate_blast_radius(self, lever_id, params):
        return self._adapter.estimate_blast_radius(lever_id, params)

    def apply(self, lever_id, params, ttl_s):
        return self._adapter.apply(lever_id, params, ttl_s)

    def undo(self, handle):
        return self._adapter.undo(handle)

    def status(self, handle):
        return self._adapter.status(handle)


class RefusingApply(_Levers):
    """The control plane rejects one lever (e.g. the retry override endpoint is down)."""

    def __init__(self, adapter, lever_id: str):
        super().__init__(adapter)
        self.lever_id = lever_id

    def apply(self, lever_id, params, ttl_s):
        if lever_id == self.lever_id:
            raise LeverError(f"{lever_id}: control plane returned 503")
        return self._adapter.apply(lever_id, params, ttl_s)


class StickyUndo(_Levers):
    """DELETE /admin/... is accepted but never takes effect: the TTL is the only safety net."""

    def undo(self, handle):
        return handle  # still active, and the adapter believes it


class SilentNoop(_Levers):
    """The control plane acknowledges every apply but nothing reaches the data plane."""

    def __init__(self, adapter):
        super().__init__(adapter)
        self._ghost = FixtureLeverAdapter()

    def apply(self, lever_id, params, ttl_s):
        return self._ghost.apply(lever_id, params, ttl_s)

    def undo(self, handle):
        return self._ghost.undo(handle)

    def status(self, handle):
        return self._ghost.status(handle)


# ---- brain chaos (the LLM side) ----------------------------------------------------------------
class _Brain:
    def __init__(self, brain):
        self._brain = brain

    def __getattr__(self, name):
        return getattr(self._brain, name)


class GarbageTriage(_Brain):
    """The LLM returned predictions for experiment ids that do not exist."""

    def triage(self, incident_id, fingerprint) -> TriageResult:
        t = self._brain.triage(incident_id, fingerprint)
        return t.model_copy(update={"predictions": [p.model_copy(update={"experiment_id": f"hallucinated_{i}"})
                                                    for i, p in enumerate(t.predictions)]})


class SwappedPredictions(_Brain):
    """The LLM attached each hypothesis's predictions to the other hypothesis. Measurement
    still decides between *the predictions it was given*, so the label can come out wrong."""

    def triage(self, incident_id, fingerprint) -> TriageResult:
        t = self._brain.triage(incident_id, fingerprint)
        ids = [h.id for h in t.hypotheses]
        swap = dict(zip(ids, ids[1:] + ids[:1]))
        return t.model_copy(update={"predictions": [p.model_copy(update={"hypothesis_id": swap[p.hypothesis_id]})
                                                    for p in t.predictions]})


class SingleHypothesis(_Brain):
    """The LLM was certain: one hypothesis, nothing to separate."""

    def triage(self, incident_id, fingerprint) -> TriageResult:
        t = self._brain.triage(incident_id, fingerprint)
        keep = t.hypotheses[0].id
        return t.model_copy(update={"ambiguous": False, "hypotheses": t.hypotheses[:1],
                                    "predictions": [p for p in t.predictions if p.hypothesis_id == keep]})


# ---- dependency chaos (clone lab, patch author, canary deployer) -------------------------------
class LabDown:
    def investigate(self, *args, **kwargs):
        raise ConnectionError("clone lab :9910 unreachable")


class LabHangs:
    def investigate(self, *args, **kwargs):
        raise TimeoutError("clone did not become healthy within 120 s")


class NothingReproduces:
    """Every clone came up healthy and stayed healthy: no hypothesis reproduces production."""

    def investigate(self, incident_id, triage, production_incident, healthy, probe):
        return [HypothesisInvestigation(h.id, f"clone-{h.id}", {"action": "noop"}, reproduced=False, recovered=False,
                                        prediction_matches=0, prediction_total=1, detail="clone stayed healthy")
                for h in triage.hypotheses]


class PatchAuthorDown:
    def propose(self, incident_id, verdict, triage) -> PatchProposal:
        raise ConnectionError("Devin API: 502 Bad Gateway")

    def revise(self, incident_id, patch, evidence):
        return None


class PatchAuthorCannotRevise:
    def propose(self, incident_id, verdict, triage) -> PatchProposal:
        return PatchProposal("devin", f"devin://task/{incident_id}", "first attempt", session_id="s1")

    def revise(self, incident_id, patch, evidence):
        return None


class CanaryBuildFails:
    def prepare(self, patch, context=None):
        raise CanaryPreparationError("orders-v2 build or startup failed: exit 1")


# ---- the catalogue -----------------------------------------------------------------------------
TARGET_CASES: list[ChaosCase] = [
    ChaosCase("storm", "hero world A: 20 s DB hiccup, retries outlast it", storm(),
              F({READY}), "H_meta"),
    ChaosCase("degraded", "hero world B: batch job halves DB capacity; a patch cannot fix hardware, so the canary must fail",
              degrade(), F({ESCALATED}), "H_db"),
    ChaosCase("cpu_starve", "none-of-the-above world: payments CPU-starved, DB is a victim",
              cpu_starve(), F({PAGED, ESCALATED}), NONE),
    ChaosCase("storm_mild", "short, shallow hiccup: does a 400 ms / 5 s blip even ignite a storm?",
              storm(delay_ms=400, duration_s=5), F({READY, NO_BREACH}), F({"H_meta"})),
    ChaosCase("storm_long", "60 s hiccup still ongoing when Faultline acts: the trigger has not left yet",
              storm(delay_ms=1500, duration_s=60), F({READY, ESCALATED, PAGED}), F({"H_meta", "H_db", NONE}), develop_s=30),
    ChaosCase("degraded_severe", "DB capacity cut to 20 qps", degrade(20), F({ESCALATED}), "H_db"),
    ChaosCase("degraded_marginal", "DB capacity 90 qps at 80 rps load: barely degraded", degrade(90),
              F({ESCALATED, PAGED, NO_BREACH}), F({"H_db", NONE, "H_meta"})),
    ChaosCase("traffic_surge", "no fault at all: traffic at 99 % of DB capacity", None,
              F({ESCALATED, PAGED, NO_BREACH}), None, load_rps=99.0),
    ChaosCase("compound", "storm ignites on top of an already degraded DB", both(degrade(60), storm()),
              F({ESCALATED, PAGED}), F({"H_db", NONE})),
    ChaosCase("heals_during_hold", "the batch job is killed while the retry cap is on: the lever gets the credit?",
              degrade(), F({READY, ESCALATED, PAGED}), None,
              schedule=((10, heal),)),
    ChaosCase("heals_after_release", "the batch job is killed during the after-release watch",
              degrade(), F({READY, ESCALATED, PAGED}), None,
              schedule=((25, heal),)),
    ChaosCase("degrades_after_release", "a storm is fixed, then a real DB degradation starts during the watch",
              storm(), F({ESCALATED, PAGED}), F({"H_db", NONE}),
              schedule=((25, degrade(40)),)),
    ChaosCase("relapse_before_canary", "storm confirmed and mitigated, then the DB degrades right before the canary",
              storm(), F({ESCALATED, PAGED}), None,
              schedule=((45, degrade(40)),)),
    ChaosCase("noisy_telemetry", "per-tick noise 4× the sandbox's: confirmation must get harder, never wronger",
              storm(), F({READY, PAGED, ESCALATED}), F({"H_meta", NONE}), prepare=noisy(0.32)),
    ChaosCase("late_detect", "Faultline only looks 110 s into the incident: almost no healthy baseline left in its 120 s window",
              storm(), F({READY, PAGED, ESCALATED}), F({"H_meta", NONE}), develop_s=110),
    ChaosCase("no_healthy_baseline", "incident older than the whole baseline window: judge has no healthy reference",
              storm(), F({PAGED}), NONE, develop_s=200),
]

RESPONDER_CASES: list[ChaosCase] = [
    ChaosCase("telemetry_gaps", "poller drops every 3rd window", storm(), F({READY, PAGED}), F({"H_meta", NONE}),
              telemetry=lambda t: DroppedWindows(t, every=3)),
    ChaosCase("telemetry_down_mid_experiment", "telemetry backend resets while the lever is applied", storm(),
              F({RAISED, PAGED}), None, telemetry=lambda t: FlakySeries(t, fail_on=2)),
    ChaosCase("telemetry_lag", "every read is 10 s stale", storm(), F({READY, PAGED, ESCALATED}), F({"H_meta", NONE}),
              telemetry=lambda t: Lagging(t, lag_s=10)),
    ChaosCase("telemetry_lag_severe", "every read is 30 s stale: after-release reads mostly see the hold", storm(),
              F({PAGED}), NONE, telemetry=lambda t: Lagging(t, lag_s=30)),
    ChaosCase("no_db_exporter", "db.* honestly missing in every window", storm(), F({READY, PAGED}), F({"H_meta", NONE}),
              telemetry=NoDbExporter),
    ChaosCase("db_counter_reset", "DB exporter restarts mid-run and reports zeros (contract violation upstream)",
              degrade(), F({ESCALATED, PAGED}), F({"H_db", NONE}),
              telemetry=lambda t: CounterResetAfter(t, at=EPOCH + timedelta(seconds=190))),
    ChaosCase("lever_refused", "retry override endpoint returns 503", storm(), F({RAISED, PAGED, REFUSED}), None,
              levers=lambda l: RefusingApply(l, "retry_cap")),
    ChaosCase("failover_refused", "db failover endpoint returns 503 in the degraded world (follow-up probe)", degrade(),
              F({RAISED, PAGED}), None, levers=lambda l: RefusingApply(l, "db_failover")),
    ChaosCase("undo_never_lands", "releases are acknowledged but never applied: TTL must clean up", storm(),
              F({ESCALATED, PAGED}), None, levers=StickyUndo),
    ChaosCase("lever_noop", "control plane says yes, data plane does nothing", storm(), F({PAGED}), NONE,
              levers=SilentNoop),
    ChaosCase("llm_garbage", "LLM predicts for experiments that do not exist", storm(), F({REFUSED}), "refused",
              brain=GarbageTriage),
    ChaosCase("llm_swapped", "LLM labels are swapped: measurement decides between the predictions it was given",
              storm(), F({READY, ESCALATED, PAGED}), None, brain=SwappedPredictions),
    ChaosCase("llm_certain", "LLM returns one hypothesis: nothing to separate", storm(), F({REFUSED, PAGED}), F({"refused", NONE}),
              brain=SingleHypothesis),
    ChaosCase("lab_down", "clone lab unreachable: v5 loop continues", storm(), F({READY}), "H_meta", investigation=LabDown()),
    ChaosCase("lab_hangs", "clone lab times out: v5 loop continues", storm(), F({READY}), "H_meta", investigation=LabHangs()),
    ChaosCase("nothing_reproduces_gated", "no hypothesis reproduces in a clone and the gate is on", storm(), F({PAGED}), NONE,
              investigation=NothingReproduces(), investigation_gate=True),
    ChaosCase("nothing_reproduces_ungated", "no hypothesis reproduces but the gate is off: evidence only", storm(),
              F({READY}), "H_meta", investigation=NothingReproduces()),
    ChaosCase("patch_author_down", "Devin API 502 after a confirmed diagnosis", storm(), F({RAISED, PAGED, ESCALATED}), None,
              patches=PatchAuthorDown()),
    ChaosCase("patch_cannot_revise", "canary regresses and the author cannot revise", degrade(), F({ESCALATED}), "H_db",
              patches=PatchAuthorCannotRevise()),
    # retry_cap stays applied for its TTL after the refused canary: that is the recorded mitigation
    # (`mitigation` event names the action), which I5 now accepts, and the run pages a human.
    ChaosCase("canary_build_fails", "orders-v2 image fails to build", storm(), F({ESCALATED}), "H_meta",
              canary=CanaryBuildFails()),
    ChaosCase("budget_exhausted", "degraded world with a budget of 2: the follow-up probe already exceeds it",
              degrade(), F({RAISED, PAGED}), None, action_budget=2),
    ChaosCase("revision_loop", "3 revisions allowed against an unfixable cause: budget must stop the loop", degrade(),
              F({RAISED, ESCALATED, PAGED}), None, max_revisions=3),
]

ALL_CASES: list[ChaosCase] = TARGET_CASES + RESPONDER_CASES
CASES_BY_ID: dict[str, ChaosCase] = {c.id: c for c in ALL_CASES}
assert len(CASES_BY_ID) == len(ALL_CASES), "duplicate chaos case id"
