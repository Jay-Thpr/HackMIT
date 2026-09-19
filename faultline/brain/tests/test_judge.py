"""Tests for the judge: math verdict over triage predictions and telemetry.

Uses the shared C2 fixtures (series_storm_experiment.json, triage_hero.json,
audit_hero.jsonl) and checks the verdict reproduces verdict_storm.json
*qualitatively*: same diagnosis, same confirmation, same leader, same
directions on the metrics that matter. Exact floats are not asserted -- the
support-scoring formula is a design choice (see judge.AGREEMENT_ODDS), not a
reproduction of whatever produced the fixture's exact numbers.
"""

import json
from pathlib import Path

from faultline_contracts.audit import AuditEvent, experiment_windows
from faultline_contracts.fingerprint import Fingerprint
from faultline_contracts.triage import Phase, TriageResult

from faultline_brain.judge import judge, phase_fingerprints
from faultline_brain.noise import NoiseModel

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "contracts" / "fixtures"


def _load_series() -> list[Fingerprint]:
    data = json.loads((FIXTURES_DIR / "series_storm_experiment.json").read_text())
    return [Fingerprint.model_validate(d) for d in data]


def _load_triage() -> TriageResult:
    data = json.loads((FIXTURES_DIR / "triage_hero.json").read_text())
    return TriageResult.model_validate(data)


def _load_windows() -> list:
    events = [
        AuditEvent.model_validate(json.loads(line))
        for line in (FIXTURES_DIR / "audit_hero.jsonl").read_text().splitlines()
        if line.strip()
    ]
    return experiment_windows(events)


HEALTHY_WINDOW_COUNT = 12  # fixture: 60s healthy prefix at WINDOW_S=5s


def _build_verdict():
    """Shared setup. The fixture is laid out as four contiguous blocks in time:
    healthy prefix, incident (pre-experiment), during, after_release. judge()
    itself slices during/after_release from the experiment window; here we only
    need to split the pre-experiment portion of the series into its healthy
    prefix (first HEALTHY_WINDOW_COUNT windows) and incident baseline (the rest,
    up to experiment_start) -- this split is a detection-stage concern the judge
    deliberately takes as given rather than inferring from telemetry alone."""
    series = _load_series()
    triage = _load_triage()
    windows = _load_windows()

    experiment_start = min(w.start for w in windows)
    pre_experiment_fps = [fp for fp in series if fp.window_end <= experiment_start]
    healthy_fps = pre_experiment_fps[:HEALTHY_WINDOW_COUNT]
    incident_fps = pre_experiment_fps[HEALTHY_WINDOW_COUNT:]

    healthy_baseline = NoiseModel.from_windows(healthy_fps)
    incident_baseline = NoiseModel.from_windows(incident_fps)

    verdict = judge(
        triage=triage,
        series=series,
        windows=windows,
        healthy_baseline=healthy_baseline,
        incident_baseline=incident_baseline,
    )
    return verdict


def test_diagnosis_is_h_meta_and_confirmed():
    verdict = _build_verdict()
    assert verdict.diagnosis == "H_meta"
    assert verdict.confirmed is True


def test_h_meta_support_is_max_and_high_confidence():
    verdict = _build_verdict()
    support_by_id = {s.hypothesis_id: s.support for s in verdict.support}

    assert support_by_id["H_meta"] == max(support_by_id.values())
    assert support_by_id["H_meta"] >= 0.8

    # supports normalize to 1 across hypotheses
    assert abs(sum(support_by_id.values()) - 1.0) < 1e-9

    meta_support = next(s for s in verdict.support if s.hypothesis_id == "H_meta")
    db_support = next(s for s in verdict.support if s.hypothesis_id == "H_db")
    assert meta_support.confirmed is True
    assert db_support.confirmed is None  # confirmation test only runs on the leader


def test_during_and_after_release_directions_match_expected_story():
    verdict = _build_verdict()
    by_key = {(o.experiment_id, o.phase, o.metric): o for o in verdict.observations}

    during_obs = by_key[("retry_cap_0_20s", Phase.during, "db.query_p50_ms")]
    after_obs = by_key[("retry_cap_0_20s", Phase.after_release, "db.query_p50_ms")]

    assert during_obs.direction.value == "down"
    assert after_obs.direction.value == "flat"


def test_observations_have_finite_z_and_matching_experiment_id():
    verdict = _build_verdict()
    assert len(verdict.observations) > 0
    for obs in verdict.observations:
        assert obs.experiment_id == "retry_cap_0_20s"
        assert obs.sigma > 0
        assert obs.z == (obs.measured - obs.baseline) / obs.sigma


def test_phase_fingerprints_slices_during_and_after_release_windows():
    series = _load_series()
    windows = _load_windows()
    window = next(w for w in windows if w.experiment_id == "retry_cap_0_20s")

    during = phase_fingerprints(series, window.start, window.release)
    after = phase_fingerprints(series, window.release, None)

    assert all(window.start <= fp.window_start < window.release for fp in during)
    assert all(fp.window_start >= window.release for fp in after)
    assert len(during) > 0
    assert len(after) > 0


def test_unrun_experiment_predictions_are_skipped_not_errored():
    """db_failover_30s never appears in audit_hero.jsonl (only retry_cap_0_20s
    ran), so its predictions for both hypotheses must be silently skipped rather
    than raising or contributing observations."""
    verdict = _build_verdict()
    assert all(o.experiment_id != "db_failover_30s" for o in verdict.observations)
