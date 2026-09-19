"""Active C2 benchmark smoke tests; C5 access is deliberately confined to bench."""

import json
from pathlib import Path

from faultline_contracts import Experiment, TriageResult
from faultline_contracts.fakes import FakeWorld
import pytest

from faultline_contracts.fault import CpuStarveFault, DegradeDbFault, StormFault

from faultline_bench import run_hero_case, run_llm_only_case, run_passive_only_case, run_random_case

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures"


def inputs():
    triage = TriageResult.model_validate_json((FIXTURES / "triage_hero.json").read_text())
    candidates = [Experiment.model_validate(item) for item in json.loads((FIXTURES / "experiments.json").read_text())]
    return triage, candidates


def test_active_loop_confirms_the_storm_with_zero_blast_retry_cap():
    triage, candidates = inputs()
    result = run_hero_case(FakeWorld(seed=1), lambda world: world.storm(StormFault()), triage, candidates)

    assert result.selected_experiment_id == "retry_cap_0_20s"
    assert result.diagnosis == "H_meta"
    assert result.confirmed is True
    assert result.blast_radius_pct == 0.0


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_retry_cap_confirms_reduced_db_because_db_stays_slow_under_reduced_load(seed):
    triage, candidates = inputs()
    result = run_hero_case(FakeWorld(seed=seed), lambda world: world.degrade_db(DegradeDbFault()), triage, candidates)

    assert result.selected_experiment_id == "retry_cap_0_20s"
    # H_db's confirmation is a positive test: DB query time stays high while the cap
    # holds load at 80/s, which neither the storm nor CPU starvation reproduces.
    assert result.diagnosis == "H_db"
    assert result.confirmed is True


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_cpu_starvation_fits_neither_hypothesis(seed):
    """None-of-the-above: the DB recovers as soon as load drops, so H_db's positive test fails,
    and the incident returns after release, so H_meta's fails too. Confirmation, not elimination."""
    triage, candidates = inputs()
    result = run_hero_case(FakeWorld(seed=seed), lambda world: world.cpu_starve(CpuStarveFault()), triage, candidates)

    assert result.selected_experiment_id == "retry_cap_0_20s"
    assert result.diagnosis == "none_of_the_above"
    assert result.confirmed is False


def test_passive_only_never_applies_an_action():
    triage, _ = inputs()
    result = run_passive_only_case(FakeWorld(seed=1), lambda world: world.storm(StormFault()), lambda _: "H_meta")
    assert result.diagnosis == "H_meta"
    assert result.selected_experiment_id is None
    assert result.action_seconds == 0
    assert result.confirmed is False


def test_random_baseline_is_seeded_and_uses_a_candidate():
    triage, candidates = inputs()
    first = run_random_case(FakeWorld(seed=1), lambda world: world.storm(StormFault()), triage, candidates, seed=7)
    second = run_random_case(FakeWorld(seed=1), lambda world: world.storm(StormFault()), triage, candidates, seed=7)
    assert first.selected_experiment_id == second.selected_experiment_id
    assert first.selected_experiment_id in {candidate.id for candidate in candidates}


def test_llm_only_is_injected_and_never_claims_math_confirmation():
    _, candidates = inputs()
    result = run_llm_only_case(
        FakeWorld(seed=1), lambda world: world.storm(StormFault()), candidates,
        choose=lambda _incident, items: items[0],
        diagnose=lambda _incident, _during, _after: "H_meta",
    )
    assert result.selected_experiment_id == "retry_cap_0_20s"
    assert result.diagnosis == "H_meta"
    assert result.confirmed is False
