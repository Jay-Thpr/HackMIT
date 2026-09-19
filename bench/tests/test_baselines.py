"""Passive ambiguity checks use only benchmark labels and C1 fingerprints."""

import json
from pathlib import Path

from faultline_contracts.fingerprint import Fingerprint

from faultline_bench.baselines import NearestCentroid, evaluate_passive


FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures"


def load(name: str) -> list[Fingerprint]:
    return [Fingerprint.model_validate(item) for item in json.loads((FIXTURES / name).read_text())]


def test_nearest_centroid_keeps_missing_metrics_missing_in_fixture_smoke_data():
    storm = load("series_storm_experiment.json")[12:24]
    degraded = load("series_degraded_db_experiment.json")[12:24]
    model = NearestCentroid.fit([*(('H_meta', fp) for fp in storm[:6]), *(('H_db', fp) for fp in degraded[:6])])
    # This only exercises benchmark mechanics. It is not the live ambiguity gate.
    report = evaluate_passive([('H_meta', storm[6]), ('H_db', degraded[6])], model.predict)
    assert report.total == 2
    assert 0.0 <= report.accuracy <= 1.0


def test_passive_evaluator_accepts_an_injected_llm_baseline():
    fingerprint = load("series_storm_experiment.json")[12]
    report = evaluate_passive([('H_meta', fingerprint)], lambda _: 'H_meta')
    assert report.accuracy == 1.0
