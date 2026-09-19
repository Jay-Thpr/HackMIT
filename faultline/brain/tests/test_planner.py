"""Planner checks against the shared hero C2/C3 fixtures."""

import json
from pathlib import Path

from faultline_contracts.levers import Experiment
from faultline_contracts.triage import TriageResult

from faultline_brain.planner import plan_experiment, score_experiment

FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "fixtures"


def hero_inputs():
    triage = TriageResult.model_validate_json((FIXTURES / "triage_hero.json").read_text())
    candidates = [Experiment.model_validate(item) for item in json.loads((FIXTURES / "experiments.json").read_text())]
    return triage, candidates


def test_hero_selects_zero_blast_retry_cap():
    triage, candidates = hero_inputs()

    plan = plan_experiment(triage, candidates)

    assert plan.selected is not None
    assert plan.selected.id == "retry_cap_0_20s"
    assert plan.scores[0].experiment.id == "retry_cap_0_20s"
    assert plan.scores[0].separation >= 3


def test_no_separation_refuses_to_pick_an_experiment():
    triage, candidates = hero_inputs()
    same_direction = triage.model_copy(deep=True)
    for prediction in same_direction.predictions:
        for expectation in prediction.during + prediction.after_release:
            expectation.direction = "flat"

    plan = plan_experiment(same_direction, candidates)

    assert plan.selected is None
    assert all(score.separation == 0 for score in plan.scores)


def test_blast_penalty_must_not_be_negative():
    triage, candidates = hero_inputs()

    try:
        score_experiment(triage.predictions, candidates[0], blast_penalty=-0.01)
    except ValueError as exc:
        assert "non-negative" in str(exc)
    else:
        raise AssertionError("negative blast penalty should be rejected")
