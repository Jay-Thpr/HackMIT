"""Noise estimation from measured, healthy telemetry windows."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean, median, pstdev

from faultline_contracts.fingerprint import Fingerprint


@dataclass(frozen=True)
class MetricNoise:
    """A metric's healthy level and conservative standard deviation."""

    baseline: float
    sigma: float
    samples: int


class NoiseModel:
    """Measured baseline statistics used by the judge.

    Sigma is never allowed to collapse to zero: it is the larger of observed
    population standard deviation and ten percent of the typical (median
    absolute) healthy value.  This implements the PRD's noise floor without
    embedding environment-specific latency thresholds.
    """

    def __init__(self, metrics: dict[str, MetricNoise]):
        self._metrics = dict(metrics)

    @classmethod
    def from_fingerprints(cls, fingerprints: list[Fingerprint]) -> "NoiseModel":
        values: dict[str, list[float]] = {}
        for fingerprint in fingerprints:
            for metric, value in fingerprint.metrics().items():
                values.setdefault(metric, []).append(value)
        return cls({metric: cls._estimate(samples) for metric, samples in values.items()})

    @staticmethod
    def _estimate(samples: list[float]) -> MetricNoise:
        if not samples:
            raise ValueError("cannot estimate noise from no samples")
        typical = median(abs(value) for value in samples)
        observed = pstdev(samples) if len(samples) > 1 else 0.0
        # A permanently-zero rate has both measured terms at zero.  The tiny
        # numerical guard only prevents division by zero; it is not an
        # operational threshold and makes any non-zero movement unmistakable.
        return MetricNoise(baseline=fmean(samples), sigma=max(observed, typical * 0.10, 1e-9), samples=len(samples))

    @property
    def metrics(self) -> dict[str, MetricNoise]:
        return dict(self._metrics)

    def get(self, metric: str) -> MetricNoise | None:
        return self._metrics.get(metric)

    def require(self, metric: str) -> MetricNoise:
        try:
            return self._metrics[metric]
        except KeyError as exc:
            raise KeyError(f"no healthy baseline available for metric {metric!r}") from exc

    def z_score(self, metric: str, measured: float, baseline: float | None = None) -> float:
        """Return a signed change in conservative sigma units.

        ``baseline`` may be an incident/pre-experiment value for directional
        predictions.  Omit it for confirmation checks against healthy state.
        """
        estimate = self.require(metric)
        reference = estimate.baseline if baseline is None else baseline
        return (measured - reference) / estimate.sigma
