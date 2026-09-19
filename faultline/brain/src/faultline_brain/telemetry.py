"""Telemetry consumption boundary for Faultline Brain.

Guards telemetry against leakage of world labels and provides readers.
"""

from datetime import datetime
from typing import Protocol

from faultline_contracts.common import WINDOW_S
from faultline_contracts.fingerprint import Fingerprint, TelemetrySource


class FairnessViolation(Exception):
    """Raised when telemetry violates fairness constraints."""

    pass


FORBIDDEN_SUBSTRINGS = ("world", "storm", "degraded", "fault", "cpu_starve")


def assert_no_leak(fp: Fingerprint) -> None:
    """Scan all string-valued fields for forbidden substrings (case-insensitive).

    Scans: service keys, slo.name, edge.src/dst, log_highlights service/level/message,
    change_events kind/target/detail.

    Raises FairnessViolation if any forbidden substring is found.
    """
    # Check service names
    for service_name in fp.services.keys():
        for forbidden in FORBIDDEN_SUBSTRINGS:
            if forbidden.lower() in service_name.lower():
                raise FairnessViolation(
                    f"Service name '{service_name}' contains forbidden substring '{forbidden}'"
                )

    # Check SLO names
    for slo in fp.slos:
        for forbidden in FORBIDDEN_SUBSTRINGS:
            if forbidden.lower() in slo.name.lower():
                raise FairnessViolation(
                    f"SLO name '{slo.name}' contains forbidden substring '{forbidden}'"
                )

    # Check edge src/dst
    for edge in fp.edges:
        for forbidden in FORBIDDEN_SUBSTRINGS:
            if forbidden.lower() in edge.src.lower():
                raise FairnessViolation(
                    f"Edge source '{edge.src}' contains forbidden substring '{forbidden}'"
                )
            if forbidden.lower() in edge.dst.lower():
                raise FairnessViolation(
                    f"Edge destination '{edge.dst}' contains forbidden substring '{forbidden}'"
                )

    # Check log highlights
    for log_h in fp.log_highlights:
        for forbidden in FORBIDDEN_SUBSTRINGS:
            if forbidden.lower() in log_h.service.lower():
                raise FairnessViolation(
                    f"Log service '{log_h.service}' contains forbidden substring '{forbidden}'"
                )
            if forbidden.lower() in log_h.level.lower():
                raise FairnessViolation(
                    f"Log level '{log_h.level}' contains forbidden substring '{forbidden}'"
                )
            if forbidden.lower() in log_h.message.lower():
                raise FairnessViolation(
                    f"Log message '{log_h.message}' contains forbidden substring '{forbidden}'"
                )

    # Check change events
    for change in fp.change_events:
        for forbidden in FORBIDDEN_SUBSTRINGS:
            if forbidden.lower() in change.kind.lower():
                raise FairnessViolation(
                    f"Change event kind '{change.kind}' contains forbidden substring '{forbidden}'"
                )
            if forbidden.lower() in change.target.lower():
                raise FairnessViolation(
                    f"Change event target '{change.target}' contains forbidden substring '{forbidden}'"
                )
            if forbidden.lower() in change.detail.lower():
                raise FairnessViolation(
                    f"Change event detail '{change.detail}' contains forbidden substring '{forbidden}'"
                )


def read_window(source: TelemetrySource, start: datetime, end: datetime) -> Fingerprint:
    """Read a single telemetry window and verify it passes fairness checks.

    Args:
        source: TelemetrySource to read from
        start: Window start time (UTC)
        end: Window end time (UTC)

    Returns:
        Fingerprint within [start, end)

    Raises:
        FairnessViolation: If telemetry contains forbidden substrings
    """
    fp = source.window(start, end)
    assert_no_leak(fp)
    return fp


def read_series(
    source: TelemetrySource, start: datetime, end: datetime, step_s: int = WINDOW_S
) -> list[Fingerprint]:
    """Read a series of telemetry windows and verify each passes fairness checks.

    Args:
        source: TelemetrySource to read from
        start: Series start time (UTC)
        end: Series end time (UTC)
        step_s: Window step in seconds (default: WINDOW_S)

    Returns:
        List of Fingerprints, each with fairness checks passed

    Raises:
        FairnessViolation: If any window contains forbidden substrings
    """
    fps = source.series(start, end, step_s)
    for fp in fps:
        assert_no_leak(fp)
    return fps


def metrics_of(fp: Fingerprint) -> dict[str, float]:
    """Extract canonical metrics from a fingerprint.

    Thin wrapper over fp.metrics() that returns dict[str, float] of canonical keys.

    Args:
        fp: Fingerprint to extract metrics from

    Returns:
        Dictionary of metric key -> float value
    """
    return fp.metrics()
