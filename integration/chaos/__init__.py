"""Chaos suite for the whole Faultline loop (detect → triage → plan → experiment → judge → mitigate →
patch → verify → canary → report), run deterministically against `FakeWorld` without Docker.

Two kinds of chaos, one set of safety invariants:

* target-system chaos — hidden C5 worlds, parameter sweeps, compound faults, faults that flip
  while an experiment is running (`chaos.cases.TARGET_CASES`);
* responder chaos — Faultline's own dependencies misbehave: telemetry gaps/lag/counter resets,
  levers that refuse or silently no-op or never release, an LLM that returns garbage or swaps
  its predictions, a clone lab that dies, a patch author that is down, an exhausted action
  budget (`chaos.cases.RESPONDER_CASES`).

Bench-side only: this package imports the hidden fault controller (C5) and must never be
imported by `faultline/` or `product/` runtime code. Faultline sees the world only through
`chaos.harness.Production`, which raises if anything reaches for a C5 method.
"""

from .cases import ALL_CASES, RESPONDER_CASES, TARGET_CASES, ChaosCase
from .harness import Outcome, check_invariants, run_case

__all__ = ["ALL_CASES", "RESPONDER_CASES", "TARGET_CASES", "ChaosCase", "Outcome", "check_invariants", "run_case"]
