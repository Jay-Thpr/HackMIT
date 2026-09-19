from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from faultline_contracts import (
    Experiment,
    Fingerprint,
    LeverSpec,
    TriageResult,
    Verdict,
)


@dataclass(frozen=True)
class PatchProposal:
    provider: str  # "devin" | "fallback"
    reference: str  # PR url / branch
    summary: str


@runtime_checkable
class Brain(Protocol):
    def triage(self, incident_id: str, fingerprint: Fingerprint) -> TriageResult: ...

    def plan(
        self,
        triage: TriageResult,
        catalog: list[LeverSpec],
        blast_radius: Callable[[str, dict], float],
    ) -> Experiment: ...

    def judge(
        self,
        triage: TriageResult,
        experiment: Experiment,
        baseline: list[Fingerprint],
        during: list[Fingerprint],
        after_release: list[Fingerprint],
    ) -> Verdict: ...


@runtime_checkable
class PatchAdapter(Protocol):
    def propose(
        self, incident_id: str, verdict: Verdict, triage: TriageResult
    ) -> PatchProposal: ...
