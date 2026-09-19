"""Faultline Brain: telemetry-ingestion boundary and anomaly detection."""

from .judge import AGREEMENT_ODDS, judge, phase_fingerprints
from .noise import DEFAULT_Z, FLOOR_FRAC, NoiseModel
from .planner import ExperimentScore, Plan, plan_experiment, score_experiment
from .telemetry import (
    FORBIDDEN_SUBSTRINGS,
    FairnessViolation,
    assert_no_leak,
    metrics_of,
    read_series,
    read_window,
)
from .triage import DEFAULT_MODEL, SYSTEM_PROMPT, TriageValidationError, run_triage

__all__ = [
    # Telemetry
    "FairnessViolation",
    "FORBIDDEN_SUBSTRINGS",
    "assert_no_leak",
    "read_window",
    "read_series",
    "metrics_of",
    # Noise
    "NoiseModel",
    "FLOOR_FRAC",
    "DEFAULT_Z",
    # Judge
    "judge",
    "phase_fingerprints",
    "AGREEMENT_ODDS",
    # Triage
    "run_triage",
    "TriageValidationError",
    "DEFAULT_MODEL",
    "SYSTEM_PROMPT",
    # Planner
    "ExperimentScore",
    "Plan",
    "score_experiment",
    "plan_experiment",
]
