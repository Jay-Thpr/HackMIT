"""Passive ambiguity checks use only benchmark labels and C1 fingerprints."""

import json
from pathlib import Path

from faultline_contracts.fingerprint import Fingerprint

from faultline_bench.baselines import NearestCentroid, evaluate_passive


FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures"


def load(name: str) -> list[Fingerprint]:
    return [Fingerprint.model_validate(item) for item in json.loads((FIXTURES / name).read_text())]


def test_nearest_centroid_keeps_missing_metrics_missing():
    storm = load("series_storm_experiment.json")[12:24]
    degraded = load("series_degraded_db_experiment.json")[12:24]
    model = NearestCentroid.fit([*(('storm', fp) for fp in storm[:6]), *(('db', fp) for fp in degraded[:6])])
    # The check is an empirical report, not a source of hidden runtime labels.
    report = evaluate_passive([('storm', storm[6]), ('db', degraded[6])], model.predict)
    assert report.total == 2
    assert 0.0 <= report.accuracy <= 1.0


def test_passive_evaluator_accepts_an_injected_llm_baseline():
    fingerprint = load("series_storm_experiment.json")[12]
    report = evaluate_passive([('storm', fingerprint)], lambda _: 'storm')
    assert report.accuracy == 1.0
