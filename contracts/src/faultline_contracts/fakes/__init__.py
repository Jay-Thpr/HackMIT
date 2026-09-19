"""In-memory fakes so every owner can build before the real sandbox exists.

NOTE: FakeWorld also implements the hidden FaultController (C5). Faultline code may use it
through the TelemetrySource / LeverAdapter protocols only; only tests/bench call its fault methods.
"""

from .fake_levers import FakeLeverAdapter, ManualClock, validate_lever_call
from .fake_telemetry import ReplayTelemetrySource, merge_fingerprints
from .sim import FakeWorld

__all__ = [
    "FakeWorld",
    "FakeLeverAdapter",
    "ReplayTelemetrySource",
    "ManualClock",
    "merge_fingerprints",
    "validate_lever_call",
]
