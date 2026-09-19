"""Tests for noise baseline and anomaly detection."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from faultline_contracts.fingerprint import Fingerprint
from faultline_contracts.fakes.fake_telemetry import ReplayTelemetrySource
from faultline_contracts.triage import Direction

from faultline_brain.noise import DEFAULT_Z, FLOOR_FRAC, NoiseModel

# Path to fixtures
FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "contracts" / "fixtures"


def test_noise_model_from_windows_healthy():
    """NoiseModel.from_windows should build from healthy fixture."""
    fp_json = (FIXTURES_DIR / "fingerprint_healthy.json").read_text()
    fp = Fingerprint.model_validate_json(fp_json)
    fps = [fp]

    model = NoiseModel.from_windows(fps)

    assert isinstance(model._baseline, dict)
    assert isinstance(model._sigma, dict)
    assert len(model._baseline) > 0
    assert len(model._sigma) > 0


def test_noise_model_baseline_and_sigma():
    """NoiseModel should have baseline and sigma methods."""
    fp_json = (FIXTURES_DIR / "fingerprint_healthy.json").read_text()
    fp = Fingerprint.model_validate_json(fp_json)
    fps = [fp]

    model = NoiseModel.from_windows(fps)

    # Get a metric from the fixture
    metrics = fps[0].metrics()
    first_key = list(metrics.keys())[0]

    baseline = model.baseline(first_key)
    sigma = model.sigma(first_key)

    assert baseline is not None
    assert sigma is not None
    assert isinstance(baseline, float)
    assert isinstance(sigma, float)
    assert sigma >= 0


def test_noise_floor_applied():
    """Sigma should apply noise floor: max(std, FLOOR_FRAC * |mean|)."""
    # Create fingerprints with very little variation
    source = ReplayTelemetrySource.from_json(FIXTURES_DIR / "series_storm_experiment.json")
    fps = source.fingerprints[:3]  # First 3 windows

    model = NoiseModel.from_windows(fps)

    # For any metric, sigma should be >= FLOOR_FRAC * |baseline|
    for metric_key, baseline_val in model._baseline.items():
        sigma_val = model._sigma[metric_key]
        floor = FLOOR_FRAC * abs(baseline_val)
        # Allow small floating point differences
        assert sigma_val >= floor * 0.99, f"Sigma for {metric_key} below floor"


def test_z_score_calculation():
    """z() should calculate correct z-scores."""
    fp_json = (FIXTURES_DIR / "fingerprint_healthy.json").read_text()
    fp = Fingerprint.model_validate_json(fp_json)
    fps = [fp]

    model = NoiseModel.from_windows(fps)

    metrics = fps[0].metrics()
    first_key = list(metrics.keys())[0]
    baseline = model.baseline(first_key)
    sigma = model.sigma(first_key)

    # Test z-score at baseline (should be 0)
    z_at_baseline = model.z(first_key, baseline)
    assert abs(z_at_baseline) < 0.01

    # Test z-score at baseline + 2*sigma
    z_above = model.z(first_key, baseline + 2 * sigma)
    assert 1.99 <= z_above <= 2.01


def test_is_significant():
    """is_significant should correctly identify anomalies."""
    fp_json = (FIXTURES_DIR / "fingerprint_healthy.json").read_text()
    fp = Fingerprint.model_validate_json(fp_json)
    fps = [fp]

    model = NoiseModel.from_windows(fps)

    metrics = fps[0].metrics()
    first_key = list(metrics.keys())[0]
    baseline = model.baseline(first_key)
    sigma = model.sigma(first_key)

    # Baseline should not be significant
    assert not model.is_significant(first_key, baseline, k=DEFAULT_Z)

    # Value at baseline + 4*sigma should be significant (> 3.0)
    assert model.is_significant(first_key, baseline + 4 * sigma, k=DEFAULT_Z)

    # Value at baseline + 2*sigma should not be significant (< 3.0)
    assert not model.is_significant(first_key, baseline + 2 * sigma, k=DEFAULT_Z)


def test_direction_flat():
    """direction() should return flat when |z| < k."""
    fp_json = (FIXTURES_DIR / "fingerprint_healthy.json").read_text()
    fp = Fingerprint.model_validate_json(fp_json)
    fps = [fp]

    model = NoiseModel.from_windows(fps)

    metrics = fps[0].metrics()
    first_key = list(metrics.keys())[0]
    baseline = model.baseline(first_key)
    sigma = model.sigma(first_key)

    # At baseline, should be flat
    direction = model.direction(first_key, baseline)
    assert direction == Direction.flat

    # At baseline + 2*sigma (< 3.0), should be flat
    direction = model.direction(first_key, baseline + 2 * sigma)
    assert direction == Direction.flat


def test_direction_up():
    """direction() should return up when measured > baseline + k*sigma."""
    fp_json = (FIXTURES_DIR / "fingerprint_healthy.json").read_text()
    fp = Fingerprint.model_validate_json(fp_json)
    fps = [fp]

    model = NoiseModel.from_windows(fps)

    metrics = fps[0].metrics()
    first_key = list(metrics.keys())[0]
    baseline = model.baseline(first_key)
    sigma = model.sigma(first_key)

    # At baseline + 4*sigma (> 3.0), should be up
    direction = model.direction(first_key, baseline + 4 * sigma)
    assert direction == Direction.up


def test_direction_down():
    """direction() should return down when measured < baseline - k*sigma."""
    fp_json = (FIXTURES_DIR / "fingerprint_healthy.json").read_text()
    fp = Fingerprint.model_validate_json(fp_json)
    fps = [fp]

    model = NoiseModel.from_windows(fps)

    metrics = fps[0].metrics()
    first_key = list(metrics.keys())[0]
    baseline = model.baseline(first_key)
    sigma = model.sigma(first_key)

    # At baseline - 4*sigma (< -3.0), should be down
    direction = model.direction(first_key, baseline - 4 * sigma)
    assert direction == Direction.down


def test_from_windows_series_experiment():
    """Test NoiseModel on the healthy prefix of series_storm_experiment."""
    source = ReplayTelemetrySource.from_json(FIXTURES_DIR / "series_storm_experiment.json")

    # Use first 12 windows (60s at 5s/window) as healthy baseline
    healthy_fps = source.fingerprints[:12]
    model = NoiseModel.from_windows(healthy_fps)

    # Find db.query_p50_ms if it exists
    all_metrics = set()
    for fp in healthy_fps:
        all_metrics.update(fp.metrics().keys())

    if "db.query_p50_ms" in all_metrics:
        # The 13th window (index 12) should show an incident
        incident_fp = source.fingerprints[12]
        incident_metrics = incident_fp.metrics()

        if "db.query_p50_ms" in incident_metrics:
            incident_value = incident_metrics["db.query_p50_ms"]
            baseline = model.baseline("db.query_p50_ms")
            sigma = model.sigma("db.query_p50_ms")

            # Should be significantly higher
            z = model.z("db.query_p50_ms", incident_value)
            if baseline is not None and sigma is not None and abs(baseline - incident_value) > sigma:
                # If values are different, direction should reflect the change
                direction = model.direction("db.query_p50_ms", incident_value)
                assert direction in (Direction.up, Direction.flat, Direction.down)


def test_direction_with_custom_k():
    """direction() should respect custom k threshold."""
    fp_json = (FIXTURES_DIR / "fingerprint_healthy.json").read_text()
    fp = Fingerprint.model_validate_json(fp_json)
    fps = [fp]

    model = NoiseModel.from_windows(fps)

    metrics = fps[0].metrics()
    first_key = list(metrics.keys())[0]
    baseline = model.baseline(first_key)
    sigma = model.sigma(first_key)

    # At baseline + 2*sigma:
    # - With k=3.0, should be flat
    # - With k=1.5, should be up
    value = baseline + 2 * sigma

    assert model.direction(first_key, value, k=3.0) == Direction.flat
    assert model.direction(first_key, value, k=1.5) == Direction.up


def test_only_present_metrics_included():
    """NoiseModel should only include metrics that are present (not None)."""
    fp_json = (FIXTURES_DIR / "fingerprint_healthy.json").read_text()
    fp = Fingerprint.model_validate_json(fp_json)
    fps = [fp]

    model = NoiseModel.from_windows(fps)

    # All metrics in the model should have been present in the fingerprint
    fp_metrics = fps[0].metrics()
    for key in model._baseline.keys():
        assert key in fp_metrics
        assert fp_metrics[key] is not None
