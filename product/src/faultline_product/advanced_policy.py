from __future__ import annotations

import math

from faultline_brain import NoiseModel
from faultline_contracts import WINDOW_S, Fingerprint


RECOVERY_METRICS = ("svc.gateway.p99_ms", "svc.gateway.error_rate", "svc.fulfillment.p99_ms")


def contiguous(windows: list[Fingerprint]) -> bool:
    return bool(windows) and all((fp.window_end - fp.window_start).total_seconds() == WINDOW_S for fp in windows) \
        and all(a.window_end == b.window_start for a, b in zip(windows, windows[1:]))


def baseline_thresholds(windows: list[Fingerprint]) -> dict[str, float]:
    if len(windows) < 24 or not contiguous(windows):
        raise ValueError("advanced detection requires at least 24 contiguous baseline windows")
    noise = NoiseModel.from_windows(windows)
    common = set.intersection(*(set(fp.metrics()) for fp in windows))
    optional = {key for key in common if key.startswith("resource.")
                and key.endswith((".lag_messages", ".outstanding", ".replication_lag_bytes"))}
    thresholds = {}
    for metric in sorted(set(RECOVERY_METRICS) | optional):
        measured = [fp.metrics().get(metric) for fp in windows]
        if any(value is None or not math.isfinite(value) or value < 0 for value in measured):
            raise ValueError(f"incomplete healthy reference for {metric}")
        typical, sigma = noise.baseline(metric), noise.sigma(metric)
        if typical is None or sigma is None:
            raise ValueError(f"missing baseline for {metric}")
        thresholds[metric] = typical + 3 * sigma
    return thresholds


def recovered(windows: list[Fingerprint], thresholds: dict[str, float]) -> bool | None:
    if len(windows) < 12 or not thresholds or not contiguous(windows[-12:]):
        return None
    measured = []
    for fp in windows[-12:]:
        metrics = fp.metrics()
        for metric, threshold in thresholds.items():
            value = metrics.get(metric)
            if value is None or not math.isfinite(value) or value < 0:
                return None
            measured.append(value <= threshold)
    return all(measured)
