"""Active C2 benchmark smoke tests; C5 access is deliberately confined to bench."""

import json
from pathlib import Path

from faultline_contracts import Experiment, TriageResult
from faultline_contracts.fakes import FakeWorld
from faultline_contracts.fault import DegradeDbFault, StormFault

from faultline_bench import run_hero_case

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


def test_retry_cap_alone_refuses_an_unconfirmed_reduced_db_diagnosis():
    triage, candidates = inputs()
    result = run_hero_case(FakeWorld(seed=2), lambda world: world.degrade_db(DegradeDbFault()), triage, candidates)

    assert result.selected_experiment_id == "retry_cap_0_20s"
    # The retry-cap response rules out the storm but does not itself satisfy
    # the simulator's full DB confirmation path. C2 must not overclaim; the
    # orchestrator can schedule db_failover as the next experiment.
    assert result.diagnosis == "none_of_the_above"
    assert result.confirmed is False
