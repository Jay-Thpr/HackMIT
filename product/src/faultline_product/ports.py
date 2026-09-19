from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
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


@dataclass(frozen=True)
class CanaryTarget:
    patch_reference: str
    version: str
    source_revision: str
    service_name: str | None = None


class CanaryStatus(str, Enum):
    passed = "passed"
    refused = "refused"
    regressed = "regressed"


class CanaryPreparationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CanaryResult:
    status: CanaryStatus
    detail: str
    target: CanaryTarget | None = None


@runtime_checkable
class Brain(Protocol):
    def triage(self, incident_id: str, fingerprint: Fingerprint) -> TriageResult: ...

    def triage_source(self) -> str | None: ...

    def plan(
        self,
        triage: TriageResult,
        catalog: list[LeverSpec],
        blast_radius: Callable[[str, dict], float],
    ) -> Experiment | None: ...

    def confirmation_experiment(
        self,
        triage: TriageResult,
        hypothesis_id: str,
        catalog: list[LeverSpec],
        blast_radius: Callable[[str, dict], float],
        excluded_ids: set[str],
    ) -> Experiment | None: ...

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


@runtime_checkable
class CanaryDeployer(Protocol):
    def prepare(self, patch: PatchProposal) -> CanaryTarget: ...


class VerificationStatus(str, Enum):
    passed = "passed"  # the patch survived the replayed incident in a clean clone
    failed = "failed"  # the incident came back or the clone never recovered
    skipped = "skipped"  # no clone lab available: fall through to the production canary (v5 path)


@dataclass(frozen=True)
class PatchVerification:
    status: VerificationStatus
    detail: str
    clone_id: str | None = None
    recipe: dict | None = None  # the lab action that replayed the reproduced incident
    evidence: dict | None = None  # measured numbers behind the decision, for the audit log / Devin


@runtime_checkable
class PatchVerifier(Protocol):
    """Stage 6b: replay the reproduced incident against the patch in a disposable clone (C6).

    Never touches production. ``diagnosis`` selects the reproduction recipe.
    """

    def verify(
        self, incident_id: str, patch: PatchProposal, diagnosis: str
    ) -> PatchVerification: ...
