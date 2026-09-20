from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from math import ceil
from pathlib import Path
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
    reference: str  # PR url / "branch:<name>" / "path:<dir>"
    summary: str
    session_id: str | None = None  # Devin session that can be asked to revise
    revision: int = 0  # 0 = first proposal, n = n-th revision after measured evidence


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

    def plan_scores(
        self,
        triage: TriageResult,
        catalog: list[LeverSpec],
        blast_radius: Callable[[str, dict], float],
    ) -> list[dict]:
        """Optional: the planner's candidate table, one row per scored experiment with
        ``experiment_id``, ``lever_id``, ``separation``, ``score``, ``blast_radius_pct``."""
        ...

    def judge(
        self,
        triage: TriageResult,
        experiment: Experiment,
        baseline: list[Fingerprint],
        during: list[Fingerprint],
        after_release: list[Fingerprint],
    ) -> Verdict: ...


@dataclass(frozen=True)
class HypothesisInvestigation:
    """Stage 4a outcome for one hypothesis, measured in its own clean clone (C6).

    Owner 3's investigator produces the evidence; this is Product's transport of it into the
    audit log, the report and the UI. ``prediction_matches``/``prediction_total`` describe how
    the clone responded to the *production probe* the planner selected.
    """

    hypothesis_id: str
    clone_id: str | None
    recipe: dict | None
    reproduced: bool
    recovered: bool
    prediction_matches: int | None
    prediction_total: int | None
    detail: str
    evidence: dict | None = None  # similarity numbers etc., for the audit log / UI
    attempts: list[dict] | None = None  # agent proposals tried, when an agent investigated

    @property
    def survives(self) -> bool:
        return (
            self.reproduced
            and self.recovered
            and (
                self.prediction_total is None
                or self.prediction_matches >= ceil(0.75 * self.prediction_total)
            )
        )


@runtime_checkable
class Investigation(Protocol):
    """Run one investigator per hypothesis in disposable clones before touching production."""

    def investigate(
        self,
        incident_id: str,
        triage: TriageResult,
        production_incident: Fingerprint,
        healthy_reference: list[Fingerprint],
        production_probe: Experiment,
    ) -> list[HypothesisInvestigation]: ...


@runtime_checkable
class PatchAdapter(Protocol):
    def propose(
        self, incident_id: str, verdict: Verdict, triage: TriageResult
    ) -> PatchProposal: ...

    def revise(
        self, incident_id: str, patch: PatchProposal, evidence: str
    ) -> PatchProposal | None:
        """Send measured evidence back to the author and return the revised patch, or None
        when the patch cannot be revised (no live session, provider does not support it)."""
        ...


@runtime_checkable
class PatchCheckout(Protocol):
    """Turn a PatchProposal.reference into a local checkout root (contains sandbox/Dockerfile)
    that the canary deployer and the clone lab can build orders-v2 from."""

    def resolve(self, patch: PatchProposal) -> Path | None: ...


@runtime_checkable
class CanaryDeployer(Protocol):
    def prepare(self, patch: PatchProposal, context: Path | None = None) -> CanaryTarget: ...


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
        self,
        incident_id: str,
        patch: PatchProposal,
        diagnosis: str,
        context: Path | None = None,
    ) -> PatchVerification: ...
