"""Passive ambiguity baselines for the benchmark only.

These evaluators receive the same steady-state C1 fingerprints as Faultline's
triage stage. Labels are benchmark outcomes, never runtime telemetry fields.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import sqrt
from typing import Callable

from faultline_contracts.fingerprint import Fingerprint


Labeler = Callable[[Fingerprint], str]


@dataclass(frozen=True)
class BaselineResult:
    label: str
    prediction: str

    @property
    def correct(self) -> bool:
        return self.label == self.prediction


@dataclass(frozen=True)
class AmbiguityReport:
    """Observed passive-baseline accuracy; callers choose their own go/no-go bar."""

    results: tuple[BaselineResult, ...]

    @property
    def accuracy(self) -> float:
        return sum(item.correct for item in self.results) / len(self.results) if self.results else 0.0

    @property
    def total(self) -> int:
        return len(self.results)


def evaluate_passive(cases: list[tuple[str, Fingerprint]], labeler: Labeler) -> AmbiguityReport:
    """Score an injected passive method (for example a no-action LLM prompt)."""
    return AmbiguityReport(tuple(BaselineResult(label, labeler(fingerprint)) for label, fingerprint in cases))


class NearestCentroid:
    """Simple passive classifier over normalized canonical C1 metrics.

    Missing metrics are excluded from each distance rather than replaced with
    zero, preserving the C1 missing-data rule. The model is benchmark-only: it
    does not expose labels or centroids to the runtime Brain.
    """

    def __init__(self, centroids: dict[str, dict[str, float]], scales: dict[str, float]):
        self._centroids = centroids
        self._scales = scales

    @classmethod
    def fit(cls, cases: list[tuple[str, Fingerprint]]) -> "NearestCentroid":
        grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        all_values: dict[str, list[float]] = defaultdict(list)
        for label, fingerprint in cases:
            for metric, value in fingerprint.metrics().items():
                grouped[label][metric].append(value)
                all_values[metric].append(value)
        if not grouped:
            raise ValueError("cannot fit nearest centroid with no cases")
        centroids = {
            label: {metric: sum(values) / len(values) for metric, values in metrics.items()}
            for label, metrics in grouped.items()
        }
        # A relative floor makes each metric dimensionless without a latency threshold.
        scales = {
            metric: max(_std(values), abs(sum(values) / len(values)) * 0.10, 1e-9)
            for metric, values in all_values.items()
        }
        return cls(centroids, scales)

    def predict(self, fingerprint: Fingerprint) -> str:
        values = fingerprint.metrics()
        distances: list[tuple[float, str]] = []
        for label, centroid in self._centroids.items():
            common = sorted(set(values) & set(centroid))
            if not common:
                continue
            distance = sqrt(sum(((values[key] - centroid[key]) / self._scales[key]) ** 2 for key in common) / len(common))
            distances.append((distance, label))
        if not distances:
            raise ValueError("fingerprint has no metrics shared with fitted centroids")
        return min(distances, key=lambda item: (item[0], item[1]))[1]


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sqrt(sum((value - mean) ** 2 for value in values) / len(values))
