"""Run the real Orchestrator + real Brain math over one FakeWorld under a chaos schedule.

The same FakeWorld object is production telemetry (C1), the lever adapter (C3) and the hidden
fault controller (C5). Faultline only ever receives `Production(world)`, a facade that exposes
the C1/C3 protocols and raises on anything else, so a run that reaches for hidden state fails
loudly instead of silently cheating.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from faultline_contracts import (
    CATALOG,
    NONE_OF_THE_ABOVE,
    ActionStatus,
    AuditEvent,
    EventKind,
    Fingerprint,
    LeverSpec,
    Stage,
    standard_blast_radius,
)
from faultline_contracts.fakes import FakeWorld
from faultline_product.adapters import FixtureCanaryDeployer, FixtureDevinAdapter, LiveBrain
from faultline_product.fixtures import load_fixture
from faultline_product.orchestrator import BudgetExceeded, Orchestrator
from faultline_product.renderer import TerminalRenderer

FINALS = ("ready", "escalated", "paged", "refused", "raised", "no_breach")
SPECS: dict[str, LeverSpec] = {spec.id: spec for spec in CATALOG}
BUNDLE = load_fixture("storm")  # canonical triage (H_meta vs H_db) + the four candidate experiments
HIDDEN_SURFACE = "hidden C5 surface"


# ---- what Faultline is allowed to see ----------------------------------------------------------
class Production:
    """TelemetrySource + LeverAdapter view of a FakeWorld. Any other attribute is a fairness leak."""

    _ALLOWED = ("window", "series", "catalog", "estimate_blast_radius", "apply", "undo", "status")

    def __init__(self, world: FakeWorld):
        self._world = world

    def __getattr__(self, name: str) -> Any:
        if name in self._ALLOWED:
            return getattr(self._world, name)
        raise AttributeError(f"Faultline touched {HIDDEN_SURFACE} {name!r}")


class MemorySink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def query(self, incident_id: str) -> list[AuditEvent]:
        return [e for e in self.events if e.incident_id == incident_id]


class ChaosClock:
    """The orchestrator's clock and sleep. Sleeping advances the simulated world one second at a
    time and fires scheduled hidden-world actions at `seconds since run start`."""

    def __init__(self, world: FakeWorld, schedule: tuple[tuple[int, Callable[[FakeWorld], object]], ...]):
        self.world = world
        self._pending = sorted(schedule, key=lambda item: item[0])
        self.t0 = world.now
        self.fired: list[tuple[datetime, str]] = []

    def __call__(self) -> datetime:
        return self.world.now

    def sleep(self, seconds: float) -> None:
        for _ in range(int(round(seconds))):
            self.world.advance(1)
            elapsed = (self.world.now - self.t0).total_seconds()
            while self._pending and self._pending[0][0] <= elapsed:
                _, action = self._pending.pop(0)
                action(self.world)
                self.fired.append((self.world.now, getattr(action, "__name__", "action")))


# ---- outcome -----------------------------------------------------------------------------------
@dataclass
class Outcome:
    case_id: str
    seed: int
    diagnosis: str | None
    final: str  # one of FINALS
    raised: str | None
    events: list[AuditEvent]
    output: list[str]
    applies: int
    breached_at_end: bool
    active_at_end: list[str]  # lever ids still active on production when the run returned
    active_after_ttl: list[str]  # ... and still active once every TTL has had time to expire
    hidden_world: str  # C5 label, for the bench-side report only
    invariant_failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.invariant_failures

    def kinds(self, stage: Stage | None = None) -> list[EventKind]:
        return [e.kind for e in self.events if stage is None or e.stage == stage]

    def has(self, kind: EventKind, stage: Stage | None = None) -> bool:
        return kind in self.kinds(stage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case_id, "seed": self.seed, "diagnosis": self.diagnosis, "final": self.final,
            "raised": self.raised, "applies": self.applies, "breached_at_end": self.breached_at_end,
            "active_at_end": self.active_at_end, "active_after_ttl": self.active_after_ttl,
            "hidden_world": self.hidden_world, "invariant_failures": self.invariant_failures,
            "events": len(self.events),
        }


def _breached(fp: Fingerprint) -> bool:
    return any(slo.breached for slo in fp.slos)


def _active(world: FakeWorld) -> list[str]:
    world._expire_actions()
    return [h.lever_id for h in world._actions.values() if h.status == ActionStatus.active]


def _final(events: list[AuditEvent], diagnosis: str | None, raised: str | None) -> str:
    if raised is not None:
        return "raised"
    if diagnosis == "refused":
        return "refused"
    report = next((e for e in reversed(events) if e.kind == EventKind.report), None)
    if report is None:
        return "paged"
    return "ready" if report.summary == "incident report ready" else "escalated"


# ---- running one case --------------------------------------------------------------------------
def run_case(case, seed: int) -> Outcome:
    """Healthy warm-up → hidden injection → develop → the real orchestrator runs on the facade."""
    world = FakeWorld(seed=seed, load_rps=case.load_rps)
    if case.prepare is not None:
        case.prepare(world)
    world.advance(case.healthy_s)
    if case.trigger is not None:
        case.trigger(world)
    world.advance(case.develop_s)
    hidden = world.state().world.value

    production = Production(world)
    telemetry = case.telemetry(production) if case.telemetry else production
    levers = case.levers(production) if case.levers else production
    brain = LiveBrain(BUNDLE.experiments, triage_fallback=BUNDLE.triage)
    if case.brain is not None:
        brain = case.brain(brain)
    clock = ChaosClock(world, case.schedule)
    audit, output = MemorySink(), []
    incident = f"chaos-{case.id}-s{seed}"
    orchestrator = Orchestrator(
        levers, audit, case.patches or FixtureDevinAdapter(), case.canary or FixtureCanaryDeployer(),
        TerminalRenderer(output.append), telemetry, brain, clock, clock.sleep, case.action_budget,
        max_revisions=case.max_revisions, investigation=case.investigation,
        investigation_gate=case.investigation_gate,
    )

    diagnosis: str | None = None
    raised: str | None = None
    if not _breached(world.latest()):
        final = "no_breach"
    else:
        try:
            diagnosis = orchestrator.run(incident, world.now).diagnosis
        except Exception as exc:  # noqa: BLE001 - chaos: we classify, the invariants judge
            raised = f"{type(exc).__name__}: {exc}"
        final = _final(audit.events, diagnosis, raised)

    active_at_end = _active(world)
    world.advance(max((SPECS[l].max_ttl_s for l in active_at_end), default=0) + 1)
    outcome = Outcome(
        case_id=case.id, seed=seed, diagnosis=diagnosis, final=final, raised=raised,
        events=audit.query(incident), output=output,
        applies=sum(e.kind == EventKind.action_apply for e in audit.events),
        breached_at_end=_breached(world.latest()), active_at_end=active_at_end,
        active_after_ttl=_active(world), hidden_world=hidden,
    )
    outcome.invariant_failures = check_invariants(outcome, case.action_budget)
    return outcome


# ---- safety invariants (hold for every case, chaos or not) -------------------------------------
def check_invariants(o: Outcome, action_budget: int) -> list[str]:
    fails: list[str] = []
    applies = [e for e in o.events if e.kind == EventKind.action_apply]
    undone = {e.action_id for e in o.events if e.kind == EventKind.action_undo}

    # I1  Faultline never reached for hidden state.
    if o.raised and HIDDEN_SURFACE in o.raised:
        fails.append(f"fairness: {o.raised}")
    # I2  Every action carries a TTL within its lever's ceiling.
    for e in applies:
        ttl, lever = e.payload.get("ttl_s"), e.payload.get("lever_id")
        if lever not in SPECS or not ttl or not 0 < ttl <= SPECS[lever].max_ttl_s:
            fails.append(f"ttl: {lever} ttl_s={ttl} (max {SPECS.get(lever) and SPECS[lever].max_ttl_s})")
    # I3  Never more than the action budget; exceeding it pages a human.
    if o.applies > action_budget:
        fails.append(f"budget: {o.applies} applies > {action_budget}")
    if o.raised and o.raised.startswith(BudgetExceeded.__name__) and not o.has(EventKind.page_human):
        fails.append("budget: exceeded without paging a human")
    # I4  Blast radius of every applied lever ≤ 50 %.
    for e in applies:
        radius = standard_blast_radius(e.payload["lever_id"], e.payload.get("params", {}))
        if radius > 50:
            fails.append(f"blast: {e.payload['lever_id']} {radius:g}% > 50%")
    # I5  Nothing stays applied except a recorded mitigation: a completed run released everything
    #     it applied unless a `mitigation` event names the action (PRD stage 5 keeps one reversible
    #     lever, e.g. db_failover holding a degraded DB up until a human fixes it); a crashed run
    #     may leave levers behind only until their TTL expires.
    if o.raised is None:
        held = {e.action_id for e in o.events if e.kind == EventKind.mitigation and e.action_id}
        held_levers = {e.payload.get("lever_id") for e in applies if e.action_id in held}
        if stray := [l for l in o.active_at_end if l not in held_levers]:
            fails.append(f"release: levers still active after a completed run: {stray}")
        if missing := [e.action_id for e in applies if e.action_id not in undone and e.action_id not in held]:
            fails.append(f"release: applies without an undo event: {len(missing)}")
    if o.active_after_ttl:
        fails.append(f"ttl: levers survived their TTL: {o.active_after_ttl}")
    # I6  A "ready" report requires a confirmed verdict, a passed canary, no page, a healthy SLO.
    if o.final == "ready":
        verdicts = [e for e in o.events if e.kind == EventKind.verdict]
        if not verdicts or not verdicts[-1].payload.get("confirmed"):
            fails.append("report: ready without a confirmed verdict")
        if o.diagnosis in (None, NONE_OF_THE_ABOVE, "refused"):
            fails.append(f"report: ready with diagnosis {o.diagnosis!r}")
        if o.has(EventKind.page_human):
            fails.append("report: ready and a human was paged")
        if o.breached_at_end:
            fails.append("report: ready while the production SLO is still breached")
    # I7  Anything that is not a clean success pages a human (a person always owns the incident).
    if o.final in ("escalated", "paged", "refused") and not o.has(EventKind.page_human):
        fails.append(f"page: final={o.final} without a page_human event")
    # I8  An unconfirmed / none-of-the-above verdict never reaches patch or canary.
    if o.diagnosis == NONE_OF_THE_ABOVE and (o.has(EventKind.patch_opened) or o.has(EventKind.action_apply, Stage.canary)):
        fails.append("gate: none_of_the_above reached patch/canary")
    return fails
