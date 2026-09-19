import json
from pathlib import Path

from faultline_contracts import Experiment, NONE_OF_THE_ABOVE, TriageResult
from faultline_contracts.fault import CpuStarveFault, DegradeDbFault, StormFault

from faultline_bench import Scenario, run_frozen_suite


FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures"


def test_frozen_suite_aggregates_active_and_injected_baselines():
    triage = TriageResult.model_validate_json((FIXTURES / "triage_hero.json").read_text())
    candidates = [Experiment.model_validate(item) for item in json.loads((FIXTURES / "experiments.json").read_text())]
    scenarios = [
        Scenario("storm", "H_meta", lambda world: world.storm(StormFault())),
        Scenario("db", "H_db", lambda world: world.degrade_db(DegradeDbFault())),
        Scenario("cpu", NONE_OF_THE_ABOVE, lambda world: world.cpu_starve(CpuStarveFault())),
    ]
    report = run_frozen_suite(scenarios, [1, 2], triage, candidates, passive_diagnose=lambda _: "H_meta")
    assert len(report.runs) == 18  # three scenarios × two seeds × active/random/passive
    assert report.accuracy("active") == 1.0
    assert set(report.to_dict()["accuracy"]) == {"active", "passive", "random"}
