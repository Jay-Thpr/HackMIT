"""Frozen, reproducible benchmark-suite orchestration and aggregate reporting."""

from collections.abc import Callable
from dataclasses import asdict, dataclass

from faultline_contracts import Experiment, TriageResult
from faultline_contracts.fakes import FakeWorld

from .runner import BenchmarkResult, ExperimentChooser, LlmDiagnoser, PassiveDiagnoser, run_hero_case, run_llm_only_case, run_passive_only_case, run_random_case

Trigger = Callable[[FakeWorld], object]


@dataclass(frozen=True)
class Scenario:
    id: str
    expected_diagnosis: str
    trigger: Trigger


@dataclass(frozen=True)
class SuiteRun:
    arm: str
    scenario: str
    seed: int
    expected_diagnosis: str
    result: BenchmarkResult

    @property
    def correct(self) -> bool:
        return self.result.diagnosis == self.expected_diagnosis


@dataclass(frozen=True)
class SuiteReport:
    runs: tuple[SuiteRun, ...]

    def accuracy(self, arm: str) -> float:
        selected = [run for run in self.runs if run.arm == arm]
        return sum(run.correct for run in selected) / len(selected) if selected else 0.0

    def to_dict(self) -> dict:
        """JSON-ready output for the overnight report; contains measured outcomes only."""
        arms = sorted({run.arm for run in self.runs})
        return {"runs": [{**asdict(run), "correct": run.correct} for run in self.runs], "accuracy": {arm: self.accuracy(arm) for arm in arms}}


def run_frozen_suite(
    scenarios: list[Scenario], seeds: list[int], triage: TriageResult, candidates: list[Experiment], *,
    passive_diagnose: PassiveDiagnoser | None = None,
    llm_choose: ExperimentChooser | None = None,
    llm_diagnose: LlmDiagnoser | None = None,
) -> SuiteReport:
    """Run benchmark arms against fresh seeded worlds; no runtime component sees C5.

    Active and random arms are always present. Passive and LLM-only arms run only
    when their callers provide a real policy, avoiding fabricated LLM results.
    """
    runs: list[SuiteRun] = []
    for scenario in scenarios:
        for seed in seeds:
            for arm, result in (
                ("active", run_hero_case(FakeWorld(seed=seed), scenario.trigger, triage, candidates)),
                ("random", run_random_case(FakeWorld(seed=seed), scenario.trigger, triage, candidates, seed=seed)),
            ):
                runs.append(SuiteRun(arm, scenario.id, seed, scenario.expected_diagnosis, result))
            if passive_diagnose is not None:
                result = run_passive_only_case(FakeWorld(seed=seed), scenario.trigger, passive_diagnose)
                runs.append(SuiteRun("passive", scenario.id, seed, scenario.expected_diagnosis, result))
            if llm_choose is not None and llm_diagnose is not None:
                result = run_llm_only_case(FakeWorld(seed=seed), scenario.trigger, candidates, llm_choose, llm_diagnose)
                runs.append(SuiteRun("llm_only", scenario.id, seed, scenario.expected_diagnosis, result))
    return SuiteReport(tuple(runs))
