"""Deterministic active-C2 benchmark execution against the shared FakeWorld.

Bench is the only C2-owned location allowed to inject a hidden fault. Runtime
Faultline never imports this module or the C5 controller.
"""

from dataclasses import dataclass
from datetime import timedelta
from random import Random
from collections.abc import Callable

from faultline_brain.judge import judge
from faultline_brain.noise import NoiseModel
from faultline_brain.planner import plan_experiment
from faultline_contracts import Experiment, TriageResult
from faultline_contracts.audit import ExperimentWindow
from faultline_contracts.fakes import FakeWorld
from faultline_contracts.fingerprint import Fingerprint

HEALTHY_S = 60
INCIDENT_S = 60
WATCH_S = 30

ExperimentChooser = Callable[[Fingerprint, list[Experiment]], Experiment | None]
PassiveDiagnoser = Callable[[Fingerprint], str]
LlmDiagnoser = Callable[[Fingerprint, list[Fingerprint], list[Fingerprint]], str]


@dataclass(frozen=True)
class BenchmarkResult:
    """One reproducible active-investigation result and its measured impact."""

    selected_experiment_id: str | None
    diagnosis: str
    confirmed: bool
    blast_radius_pct: float | None
    action_seconds: int
    # Common reporting fields. Live runners may populate request impact and LLM
    # usage; deterministic worlds deliberately leave those unknown.
    time_to_verdict_seconds: int | None = None
    actions_applied: int = 0
    requests_affected: int | None = None
    token_usage: int | None = None
    estimated_cost_usd: float | None = None


def run_hero_case(
    world: FakeWorld,
    trigger,
    triage: TriageResult,
    candidates: list[Experiment],
    *,
    chooser: ExperimentChooser | None = None,
) -> BenchmarkResult:
    """Run the C2 plan/judge loop over one seeded world.

    The caller supplies the trigger so benchmark suites can vary injected
    conditions without exposing hidden state to runtime Brain code.
    """
    world.advance(HEALTHY_S)
    trigger(world)
    world.advance(INCIDENT_S)
    selected = chooser(world.latest(), candidates) if chooser else plan_experiment(triage, candidates).selected
    if selected is None:
        return BenchmarkResult(None, "none_of_the_above", False, None, 0, HEALTHY_S + INCIDENT_S, 0)

    if selected.id not in {candidate.id for candidate in candidates}:
        raise ValueError("benchmark chooser returned an experiment outside the candidate set")

    return _run_measured_experiment(world, selected, triage)


def run_random_case(
    world: FakeWorld, trigger, triage: TriageResult, candidates: list[Experiment], *, seed: int
) -> BenchmarkResult:
    """Random-lever ablation using a supplied seed for reproducible comparisons."""
    rng = Random(seed)
    return run_hero_case(world, trigger, triage, candidates, chooser=lambda _incident, items: rng.choice(items) if items else None)


def run_passive_only_case(world: FakeWorld, trigger, diagnose: PassiveDiagnoser) -> BenchmarkResult:
    """Stage-3-only baseline: observes an incident and never applies a lever."""
    world.advance(HEALTHY_S)
    trigger(world)
    world.advance(INCIDENT_S)
    return BenchmarkResult(None, diagnose(world.latest()), False, None, 0, HEALTHY_S + INCIDENT_S, 0)


def run_llm_only_case(
    world: FakeWorld,
    trigger,
    candidates: list[Experiment],
    choose: ExperimentChooser,
    diagnose: LlmDiagnoser,
) -> BenchmarkResult:
    """LLM-only ablation: it chooses and interprets an action without the math judge.

    Both callbacks are injected; production benchmarking can supply an OpenAI
    client while deterministic tests use a stub. Its diagnosis is intentionally
    unconfirmed because it has not passed C2's measurement gate.
    """
    world.advance(HEALTHY_S)
    trigger(world)
    world.advance(INCIDENT_S)
    incident = world.latest()
    selected = choose(incident, candidates)
    if selected is None:
        return BenchmarkResult(None, diagnose(incident, [], []), False, None, 0, HEALTHY_S + INCIDENT_S, 0)
    if selected.id not in {candidate.id for candidate in candidates}:
        raise ValueError("LLM-only chooser returned an experiment outside the candidate set")
    start = world.now
    handle = world.apply(selected.lever_id, selected.params, ttl_s=selected.hold_s + WATCH_S + 10)
    world.advance(selected.hold_s)
    release = world.now
    world.undo(handle)
    world.advance(WATCH_S)
    series = world.series(start, world.now)
    during = [fp for fp in series if fp.window_start < release]
    after = [fp for fp in series if fp.window_start >= release]
    return BenchmarkResult(selected.id, diagnose(incident, during, after), False, selected.blast_radius_pct, selected.hold_s, HEALTHY_S + INCIDENT_S + selected.hold_s + WATCH_S, 1)


def _run_measured_experiment(world: FakeWorld, experiment: Experiment, triage: TriageResult) -> BenchmarkResult:
    """Execute the C3 action and let the C2 mathematical judge evaluate it."""
    start = world.now
    handle = world.apply(experiment.lever_id, experiment.params, ttl_s=experiment.hold_s + WATCH_S + 10)
    world.advance(experiment.hold_s)
    release = world.now
    world.undo(handle)
    world.advance(WATCH_S)

    series = world.series(world.start, world.now)
    healthy = series[: HEALTHY_S // 5]
    incident = series[HEALTHY_S // 5 : (HEALTHY_S + INCIDENT_S) // 5]
    verdict = judge(
        triage,
        series,
        [ExperimentWindow(experiment_id=experiment.id, start=start, release=release)],
        NoiseModel.from_windows(healthy),
        NoiseModel.from_windows(incident),
    )
    return BenchmarkResult(
        selected_experiment_id=experiment.id,
        diagnosis=verdict.diagnosis,
        confirmed=verdict.confirmed,
        blast_radius_pct=experiment.blast_radius_pct,
        action_seconds=experiment.hold_s,
        time_to_verdict_seconds=HEALTHY_S + INCIDENT_S + experiment.hold_s + WATCH_S,
        actions_applied=1,
    )
