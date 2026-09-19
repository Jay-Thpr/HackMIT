"""Faultline's evidence-based incident brain.

The package deliberately knows only the C1--C3/C2 contract types.  It does not
know which sandbox world produced a fingerprint; that boundary is important to
the benchmark as well as to production safety.
"""

from .judge import Judge, PhaseMeasurement
from .noise import MetricNoise, NoiseModel
from .planner import ExperimentPlan, ExperimentPlanner, ExperimentScore
from .triage import OpenAITriageClient, TriageError

__all__ = [
    "ExperimentPlan",
    "ExperimentPlanner",
    "ExperimentScore",
    "Judge",
    "MetricNoise",
    "NoiseModel",
    "OpenAITriageClient",
    "PhaseMeasurement",
    "TriageError",
]
