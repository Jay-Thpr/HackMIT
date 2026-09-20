from __future__ import annotations

import math
from datetime import datetime

from faultline_contracts import WINDOW_S, Fingerprint


def healthy_window_issue(windows: list[Fingerprint], *, minimum_windows: int,
                         service: str = "orders_v2") -> str | None:
    if len(windows) < minimum_windows:
        return f"incomplete telemetry: {len(windows)}/{minimum_windows} windows"
    ordered = sorted(windows, key=lambda fp: fp.window_start)
    previous_end: datetime | None = None
    for fp in ordered:
        duration = (fp.window_end - fp.window_start).total_seconds()
        if duration != WINDOW_S or (previous_end is not None and fp.window_start != previous_end):
            return "telemetry windows overlap, have gaps, or do not span five seconds"
        previous_end = fp.window_end
        slo = next((item for item in fp.slos if item.name == "checkout"), None)
        if (slo is None or slo.value is None or not math.isfinite(slo.value)
                or not math.isfinite(slo.threshold) or slo.threshold <= 0):
            return "missing finite checkout SLO evidence"
        if slo.breached or slo.value > slo.threshold:
            return "checkout SLO breached"
        for name in ("gateway", service):
            stats = fp.services.get(name)
            if (stats is None or stats.qps is None or not math.isfinite(stats.qps) or stats.qps <= 0
                    or stats.p99_ms is None or not math.isfinite(stats.p99_ms) or stats.p99_ms < 0
                    or stats.error_rate is None or not math.isfinite(stats.error_rate)
                    or not 0 <= stats.error_rate <= 1):
                return f"missing traffic, latency, or error evidence for {name}"
            if stats.p99_ms > slo.threshold:
                return f"{name} p99 exceeds the checkout SLO"
            if stats.error_rate > 0:
                return f"{name} reported errors in the prepared-demo healthy window"
    return None


def canary_split_evidence(windows: list[Fingerprint], *, expected_share: float = 0.05,
                          service: str = "orders_v2") -> dict:
    if not math.isfinite(expected_share) or not 0 < expected_share < 1:
        raise ValueError("canary share must be finite and strictly between zero and one")
    totals = {"orders": 0.0, service: 0.0}
    for fp in windows:
        duration = (fp.window_end - fp.window_start).total_seconds()
        if not math.isfinite(duration) or duration <= 0:
            return {"canary_split_status": "missing"}
        for name in totals:
            stats = fp.services.get(name)
            if stats is None or stats.qps is None or not math.isfinite(stats.qps) or stats.qps < 0:
                return {"canary_split_status": "missing"}
            totals[name] += stats.qps * duration
    total = sum(totals.values())
    if total <= 0:
        return {"canary_split_status": "missing"}
    observed = totals[service] / total
    tolerance = 3 * math.sqrt(expected_share * (1 - expected_share) / total)
    return {
        "canary_split_status": "compatible" if abs(observed - expected_share) <= tolerance else "mismatched",
        "canary_expected_share": expected_share,
        "canary_observed_share_estimate": observed,
        "canary_total_request_estimate": total,
        "canary_v2_request_estimate": totals[service],
        "canary_share_three_sigma": tolerance,
        "canary_split_source": "C1 QPS integrals; request-count estimates, not exact counts",
    }


def canary_window_issue(windows: list[Fingerprint], *, minimum_windows: int,
                        expected_share: float = 0.05, service: str = "orders_v2") -> str | None:
    for name in ("orders", service):
        issue = healthy_window_issue(windows, minimum_windows=minimum_windows, service=name)
        if issue is not None:
            return issue
    evidence = canary_split_evidence(windows, expected_share=expected_share, service=service)
    if evidence["canary_split_status"] == "missing":
        return "missing measured canary traffic split"
    if evidence["canary_split_status"] != "compatible":
        return "measured canary traffic split differs from the requested share"
    return None
