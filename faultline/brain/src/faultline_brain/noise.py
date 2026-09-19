"""Noise baseline and anomaly detection for telemetry metrics."""

from dataclasses import dataclass
from math import sqrt

from faultline_contracts.fingerprint import Fingerprint
from faultline_contracts.triage import Direction

# Noise floor: sigma = max(measured_std, FLOOR_FRAC * |typical|)
FLOOR_FRAC = 0.10

# Significance threshold: |z| >= DEFAULT_Z is anomalous
DEFAULT_Z = 3.0


@dataclass(frozen=True)
class NoiseModel:
    """Baseline and sigma (noise) for each metric."""

    _baseline: dict[str, float]
    _sigma: dict[str, float]

    @classmethod
    def from_windows(cls, fps: list[Fingerprint]) -> "NoiseModel":
        """Build a noise model from a series of fingerprints.

        For each metric present across windows:
        - baseline = mean value
        - sigma = max(measured std, FLOOR_FRAC * |mean|)

        Only metrics that are present (non-None) are included.

        Args:
            fps: List of fingerprints to build model from

        Returns:
            NoiseModel with baseline and sigma per metric
        """
        # Collect all metric values per key
        metric_values: dict[str, list[float]] = {}

        for fp in fps:
            metrics = fp.metrics()
            for key, value in metrics.items():
                if value is not None:
                    if key not in metric_values:
                        metric_values[key] = []
                    metric_values[key].append(value)

        baseline: dict[str, float] = {}
        sigma: dict[str, float] = {}

        for key, values in metric_values.items():
            if not values:
                continue

            # Calculate mean (baseline)
            mean = sum(values) / len(values)
            baseline[key] = mean

            # Calculate standard deviation
            if len(values) == 1:
                std = 0.0
            else:
                variance = sum((v - mean) ** 2 for v in values) / len(values)
                std = sqrt(variance)

            # Apply noise floor
            floor = FLOOR_FRAC * abs(mean)
            sigma[key] = max(std, floor)

        return cls(_baseline=baseline, _sigma=sigma)

    def baseline(self, metric: str) -> float | None:
        """Get baseline value for a metric.

        Args:
            metric: Metric key

        Returns:
            Baseline value, or None if metric not in model
        """
        return self._baseline.get(metric)

    def sigma(self, metric: str) -> float | None:
        """Get noise (sigma) for a metric.

        Args:
            metric: Metric key

        Returns:
            Sigma value, or None if metric not in model
        """
        return self._sigma.get(metric)

    def z(self, metric: str, measured: float) -> float:
        """Calculate z-score for a measured value.

        z = (measured - baseline) / sigma

        Args:
            metric: Metric key
            measured: Observed value

        Returns:
            Z-score (absolute value in terms of sigmas from baseline)

        Raises:
            KeyError: If metric not in model
        """
        baseline = self._baseline[metric]
        sigma_val = self._sigma[metric]

        if sigma_val == 0:
            # If sigma is 0, treat as infinite z (always significant if changed)
            return float("inf") if measured != baseline else 0.0

        return (measured - baseline) / sigma_val

    def is_significant(self, metric: str, measured: float, k: float = DEFAULT_Z) -> bool:
        """Check if a measured value is significantly different from baseline.

        A value is significant if |z| >= k.

        Args:
            metric: Metric key
            measured: Observed value
            k: Significance threshold in sigmas (default: DEFAULT_Z)

        Returns:
            True if |z| >= k

        Raises:
            KeyError: If metric not in model
        """
        z_score = self.z(metric, measured)
        return abs(z_score) >= k

    def direction(
        self, metric: str, measured: float, k: float = DEFAULT_Z
    ) -> Direction:
        """Determine direction of change from baseline.

        Direction is:
        - flat: if |z| < k
        - up: if z >= k (measured > baseline)
        - down: if z <= -k (measured < baseline)

        Args:
            metric: Metric key
            measured: Observed value
            k: Significance threshold in sigmas (default: DEFAULT_Z)

        Returns:
            Direction.flat, Direction.up, or Direction.down

        Raises:
            KeyError: If metric not in model
        """
        z_score = self.z(metric, measured)

        if abs(z_score) < k:
            return Direction.flat

        return Direction.up if z_score >= k else Direction.down
