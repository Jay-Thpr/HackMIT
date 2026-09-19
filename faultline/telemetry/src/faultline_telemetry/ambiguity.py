"""Label-free C1 exports for passive ambiguity checks.

This module intentionally exports only the canonical metrics derived from C1
fingerprints.  It has no world, fault, trigger, or clone-cause fields, so a
passive classifier cannot learn the answer from telemetry metadata.
"""

from datetime import datetime
from typing import Any

from faultline_contracts.common import WINDOW_S
from faultline_contracts.fingerprint import Fingerprint, TelemetrySource


def ambiguity_rows(fingerprints: list[Fingerprint]) -> list[dict[str, Any]]:
    """Return stable, JSON-ready metric rows for centroid or LLM evaluation."""
    return [
        {
            "window_start": fingerprint.window_start.isoformat(),
            "window_end": fingerprint.window_end.isoformat(),
            "metrics": fingerprint.metrics(),
        }
        for fingerprint in sorted(fingerprints, key=lambda item: item.window_start)
    ]


def export_ambiguity_window(
    source: TelemetrySource, start: datetime, end: datetime, step_s: int = WINDOW_S
) -> list[dict[str, Any]]:
    """Read a C1 series and turn it into a passive, label-free dataset."""
    if step_s != WINDOW_S:
        raise ValueError(f"C1 requires {WINDOW_S}s windows")
    return ambiguity_rows(source.series(start, end, step_s))
