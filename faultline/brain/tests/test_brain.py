import json
from pathlib import Path

from faultline_contracts.fingerprint import Fingerprint
from faultline_contracts.levers import Experiment
from faultline_contracts.triage import NONE_OF_THE_ABOVE, Phase, TriageResult

from faultline.brain.judge import Judge, PhaseMeasurement
from faultline.brain.noise import NoiseModel
from faultline.brain.planner import ExperimentPlanner


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "contracts" / "fixtures"


def load(name, model):
    return model.model_validate(json.loads((FIXTURES / name).read_text()))


def fingerprints(name):
    return [Fingerprint.model_validate(item) for item in json.loads((FIXTURES / name).read_text())]


def test_noise_has_a_floor_and_omits_missing_metrics():
    series = fingerprints("series_storm_experiment.json")
    noise = NoiseModel.from_fingerprints(series[:12])
    db = noise.require("db.query_p50_ms")
    assert db.sigma >= abs(db.baseline) * 0.10
    assert noise.get("svc.orders.p50_ms") is None


def test_planner_prefers_retry_cap_over_a_more_disruptive_tie():
    draft = load("triage_hero.json", TriageResult)
    experiments = [Experiment.model_validate(item) for item in json.loads((FIXTURES / "experiments.json").read_text())]
    plan = ExperimentPlanner().choose(draft, experiments)
    assert plan.experiment.id == "retry_cap_0_20s"
    assert plan.score.separation > 0


def test_judge_confirms_storm_only_after_release_stays_healthy():
    draft = load("triage_hero.json", TriageResult)
    series = fingerprints("series_storm_experiment.json")
    result = Judge().verdict(
        draft.incident_id,
        draft,
        "retry_cap_0_20s",
        NoiseModel.from_fingerprints(series[:12]),
        [
            PhaseMeasurement(Phase.during, series[24:28], reference=series[23]),
            PhaseMeasurement(Phase.after_release, series[28:]),
        ],
    )
    assert result.diagnosis == "H_meta"
    assert result.confirmed


def test_judge_confirms_db_when_retries_return_after_release():
    draft = load("triage_hero.json", TriageResult)
    series = fingerprints("series_degraded_db_experiment.json")
    result = Judge().verdict(
        draft.incident_id,
        draft,
        "retry_cap_0_20s",
        NoiseModel.from_fingerprints(series[:12]),
        [
            PhaseMeasurement(Phase.during, series[24:28], reference=series[23]),
            PhaseMeasurement(Phase.after_release, series[28:]),
        ],
    )
    assert result.diagnosis == "H_db"
    assert result.confirmed


def test_judge_reports_none_of_the_above_when_no_confirmation_passes():
    draft = load("triage_hero.json", TriageResult)
    series = fingerprints("series_storm_experiment.json")
    result = Judge().verdict(
        draft.incident_id,
        draft,
        "retry_cap_0_20s",
        NoiseModel.from_fingerprints(series[:12]),
        [PhaseMeasurement(Phase.during, series[12:16], reference=series[12])],
    )
    assert result.diagnosis == NONE_OF_THE_ABOVE
    assert not result.confirmed
