"""Tests for telemetry consumption boundary."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from faultline_contracts.fakes.fake_telemetry import ReplayTelemetrySource
from faultline_contracts.fingerprint import Fingerprint

from faultline_brain.telemetry import (
    FORBIDDEN_SUBSTRINGS,
    FairnessViolation,
    assert_no_leak,
    metrics_of,
    read_series,
    read_window,
)

# Path to fixtures
FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "contracts" / "fixtures"


def test_forbidden_substrings_defined():
    """Verify FORBIDDEN_SUBSTRINGS contains expected values."""
    assert "world" in FORBIDDEN_SUBSTRINGS
    assert "storm" in FORBIDDEN_SUBSTRINGS
    assert "degraded" in FORBIDDEN_SUBSTRINGS
    assert "fault" in FORBIDDEN_SUBSTRINGS
    assert "cpu_starve" in FORBIDDEN_SUBSTRINGS


def test_assert_no_leak_clean_fixture():
    """assert_no_leak should pass on clean fixtures."""
    fp = Fingerprint.model_validate_json((FIXTURES_DIR / "fingerprint_healthy.json").read_text())
    # Should not raise
    assert_no_leak(fp)


def test_assert_no_leak_raises_on_service_name():
    """assert_no_leak should raise if a service name contains a forbidden substring."""
    fp = Fingerprint.model_validate_json((FIXTURES_DIR / "fingerprint_healthy.json").read_text())

    # Modify a service to have a forbidden substring
    fp.services["storm_service"] = fp.services.pop("gateway")

    with pytest.raises(FairnessViolation, match="Service name.*contains forbidden substring"):
        assert_no_leak(fp)


def test_assert_no_leak_raises_on_log_message():
    """assert_no_leak should raise if a log message contains a forbidden substring."""
    fp = Fingerprint.model_validate_json((FIXTURES_DIR / "fingerprint_healthy.json").read_text())

    # Add a log highlight with forbidden substring
    from faultline_contracts.fingerprint import LogHighlight

    fp.log_highlights.append(
        LogHighlight(service="gateway", level="error", message="system under storm", count=1)
    )

    with pytest.raises(FairnessViolation, match="Log message.*contains forbidden substring"):
        assert_no_leak(fp)


def test_assert_no_leak_case_insensitive():
    """assert_no_leak should detect forbidden substrings case-insensitively."""
    fp = Fingerprint.model_validate_json((FIXTURES_DIR / "fingerprint_healthy.json").read_text())

    # Add a log with uppercase STORM
    from faultline_contracts.fingerprint import LogHighlight

    fp.log_highlights.append(
        LogHighlight(service="gateway", level="error", message="STORM occurred", count=1)
    )

    with pytest.raises(FairnessViolation):
        assert_no_leak(fp)


def test_metrics_of_returns_canonical_keys():
    """metrics_of should return metrics with canonical keys."""
    fp = Fingerprint.model_validate_json((FIXTURES_DIR / "fingerprint_healthy.json").read_text())
    metrics = metrics_of(fp)

    # Check that we have metrics
    assert isinstance(metrics, dict)
    assert len(metrics) > 0

    # Check canonical key patterns
    for key in metrics.keys():
        assert isinstance(key, str)
        # All keys should start with svc., db, edge., or slo.
        assert key.startswith(("svc.", "db.", "edge.", "slo."))


def test_read_window_returns_fingerprint():
    """read_window should return a Fingerprint."""
    from datetime import timedelta

    source = ReplayTelemetrySource.from_json(FIXTURES_DIR / "series_storm_experiment.json")
    start = source.start
    end = start + timedelta(seconds=5)

    fp = read_window(source, start, end)

    assert isinstance(fp, Fingerprint)
    assert fp.window_start == start


def test_read_series_returns_list():
    """read_series should return a list of Fingerprints."""
    source = ReplayTelemetrySource.from_json(FIXTURES_DIR / "series_storm_experiment.json")
    start = source.start
    end = source.end

    fps = read_series(source, start, end, step_s=5)

    assert isinstance(fps, list)
    assert len(fps) > 0
    assert all(isinstance(fp, Fingerprint) for fp in fps)


def test_read_series_applies_fairness_check():
    """read_series should apply fairness checks to each window."""
    source = ReplayTelemetrySource.from_json(FIXTURES_DIR / "series_storm_experiment.json")
    start = source.start
    end = source.end

    # This should not raise on the healthy series
    fps = read_series(source, start, end, step_s=5)

    # All windows should have passed fairness checks
    assert len(fps) > 0
    for fp in fps:
        # Re-checking should not raise
        assert_no_leak(fp)
