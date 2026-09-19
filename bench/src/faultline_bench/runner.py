"""Deterministic active-C2 benchmark execution against the shared FakeWorld.

Bench is the only C2-owned location allowed to inject a hidden fault. Runtime
Faultline never imports this module or the C5 controller.
"""

from dataclasses import dataclass
from datetime import timedelta

from faultline_brain.judge import judge
from faultline_brain.noise import NoiseModel
from faultline_brain.planner import plan_experiment
from faultline_contracts import Experiment, TriageResult
from faultline_contracts.audit import ExperimentWindow
from faultline_contracts.fakes import FakeWorld

HEALTHY_S = 60
INCIDENT_S = 60
WATCH_S = 30


@dataclass(frozen=True)
class BenchmarkResult:
    """One reproducible active-investigation result and its measured impact."""

    selected_experiment_id: str | None
    diagnosis: str
    confirmed: bool
    blast_radius_pct: float | None
    action_seconds: int


def run_hero_case(
    world: FakeWorld,
    trigger,
    triage: TriageResult,
    candidates: list[Experiment],
) -> BenchmarkResult:
    """Run the C2 plan/judge loop over one seeded world.

    The caller supplies the trigger so benchmark suites can vary injected
    conditions without exposing hidden state to runtime Brain code.
    """
    world.advance(HEALTHY_S)
    trigger(world)
    world.advance(INCIDENT_S)
    plan = plan_experiment(triage, candidates)
    if plan.selected is None:
        return BenchmarkResult(None, "none_of_the_above", False, None, 0)

    experiment = plan.selected
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
    )
