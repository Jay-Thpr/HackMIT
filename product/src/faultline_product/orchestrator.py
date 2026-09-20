import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from faultline_contracts import (
    NONE_OF_THE_ABOVE,
    WINDOW_S,
    ActionHandle,
    Actor,
    AuditEvent,
    AuditSink,
    EventKind,
    Experiment,
    Fingerprint,
    LeverAdapter,
    LeverError,
    Stage,
    TelemetrySource,
    TriageResult,
    Verdict,
    utcnow,
)

from .ports import (
    Brain,
    CanaryDeployer,
    CanaryPreparationError,
    CanaryResult,
    CanaryStatus,
    CanaryTarget,
    HypothesisInvestigation,
    Investigation,
    PatchAdapter,
    PatchCheckout,
    PatchProposal,
    PatchVerification,
    PatchVerifier,
    VerificationStatus,
)
from .renderer import TerminalRenderer

BASELINE_S = 120
CODE_SUPERSEDES = {"retry_cap"}  # levers whose job the durable code patch takes over


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    incident_id: str
    diagnosis: str
    patch: PatchProposal | None
    canary: CanaryResult | None = None
    verification: PatchVerification | None = None
    investigations: list[HypothesisInvestigation] | None = None


class Orchestrator:
    def __init__(
        self,
        levers: LeverAdapter,
        audit: AuditSink,
        patches: PatchAdapter,
        canary_deployer: CanaryDeployer,
        renderer: TerminalRenderer,
        telemetry: TelemetrySource,
        brain: Brain,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        action_budget: int = 5,
        verifier: PatchVerifier | None = None,
        checkout: PatchCheckout | None = None,
        max_revisions: int = 1,
        investigation: Investigation | None = None,
        investigation_gate: bool = False,
    ):
        self._levers, self._audit, self._patches = levers, audit, patches
        self._canary_deployer = canary_deployer
        self._verifier = verifier
        self._checkout = checkout
        self._max_revisions = max_revisions
        self._investigation = investigation
        self._investigation_gate = investigation_gate
        self._renderer, self._telemetry, self._brain = renderer, telemetry, brain
        self._clock, self._sleep, self._action_budget = clock, sleep, action_budget
        self._actions = 0
        self._incident_id = ""

    def run(self, incident_id: str, now: datetime | None = None) -> RunResult:
        self._incident_id = incident_id
        now = now or self._clock()
        fp = self.detect(incident_id, now)
        triage = self.triage(incident_id, fp)
        experiment = self.plan(incident_id, triage)
        if experiment is None:
            self._record(
                incident_id,
                Stage.report,
                EventKind.report,
                Actor.orchestrator,
                "incident report ready",
                {"diagnosis": "refused"},
            )
            self._renderer.event("report", f"ready: faultline report --incident {incident_id}")
            return RunResult(incident_id, "refused", None)
        investigations, triage = self.investigate(incident_id, triage, fp, experiment, now)
        if triage is None:
            return RunResult(incident_id, NONE_OF_THE_ABOVE, None, investigations=investigations)
        baseline, during, after_release = self.experiment(incident_id, experiment, now)
        verdict = self.judge(incident_id, triage, experiment, baseline, during, after_release)
        if not verdict.confirmed:
            follow_up = self.confirmation_experiment(incident_id, triage, verdict, experiment)
            if follow_up is not None:
                experiment = follow_up
                baseline, during, after_release = self.experiment(
                    incident_id, experiment, self._clock()
                )
                verdict = self.judge(
                    incident_id, triage, experiment, baseline, during, after_release
                )
        if verdict.diagnosis == NONE_OF_THE_ABOVE or not verdict.confirmed:
            self._record(
                incident_id,
                Stage.mitigate,
                EventKind.page_human,
                Actor.orchestrator,
                "diagnosis not confirmed",
                {},
            )
            return RunResult(incident_id, verdict.diagnosis, None, investigations=investigations)
        mitigation = self.mitigate(incident_id, triage, experiment, verdict)
        patch = self.patch(incident_id, verdict, triage)
        patch, verification, canary = self.ship(incident_id, patch, verdict.diagnosis, mitigation)
        report_ready = canary.status == CanaryStatus.passed
        report_summary = (
            "incident report ready"
            if report_ready
            else f"incident escalated: canary {canary.status.value}"
        )
        self._record(
            incident_id,
            Stage.report,
            EventKind.report,
            Actor.orchestrator,
            report_summary,
            {
                "diagnosis": verdict.diagnosis,
                "patch_reference": patch.reference,
                "clone_verification": verification.status.value,
                "canary_status": canary.status.value,
                "canary_detail": canary.detail,
            },
        )
        report_label = "ready" if report_ready else "escalated"
        self._renderer.event(
            "report", f"{report_label}: faultline report --incident {incident_id}"
        )
        return RunResult(incident_id, verdict.diagnosis, patch, canary, verification, investigations)

    def detect(self, incident_id: str, now: datetime) -> Fingerprint:
        fp = self._telemetry.window(now - timedelta(seconds=WINDOW_S), now)
        breached = next((slo for slo in fp.slos if slo.breached), None)
        if breached is None:
            raise ValueError("orchestration requires a breached SLO fingerprint")
        self._record(
            incident_id,
            Stage.detect,
            EventKind.detect,
            Actor.orchestrator,
            f"{breached.name} SLO breached",
            {"metric": breached.metric, "value": breached.value, "threshold": breached.threshold},
        )
        self._renderer.event("detect", f"{breached.name} SLO breached")
        return fp

    def triage(self, incident_id: str, fingerprint: Fingerprint) -> TriageResult:
        triage = self._brain.triage(incident_id, fingerprint)
        ids = [item.id for item in triage.hypotheses]
        self._record(
            incident_id,
            Stage.triage,
            EventKind.triage,
            Actor.llm,
            f"ambiguous: {' vs '.join(ids)}",
            {"hypotheses": ids, "ambiguous": triage.ambiguous},
        )
        self._renderer.event("triage", f"ambiguous: {' vs '.join(ids)}")
        source = self._brain.triage_source()
        if source:
            self._renderer.event("triage", f"source: {source}")
        return triage

    def plan(self, incident_id: str, triage: TriageResult) -> Experiment | None:
        experiment = self._brain.plan(
            triage, self._levers.catalog(), self._levers.estimate_blast_radius
        )
        if experiment is None:
            self._record(
                incident_id,
                Stage.experiment,
                EventKind.refused,
                Actor.orchestrator,
                "no experiment separates the hypotheses",
                {"hypotheses": [hypothesis.id for hypothesis in triage.hypotheses]},
            )
            self._record(
                incident_id,
                Stage.experiment,
                EventKind.page_human,
                Actor.orchestrator,
                "experiment unavailable; page human",
                {},
            )
            self._renderer.event("plan", "no separating experiment — paged human")
            return None
        radius = experiment.blast_radius_pct
        self._renderer.event("plan", f"{experiment.id} selected, blast radius {radius:g}%")
        if radius > 50:
            self._record(
                incident_id,
                Stage.experiment,
                EventKind.refused,
                Actor.orchestrator,
                f"blast radius {radius:g}% exceeds 50%",
                {"blast_radius_pct": radius, "experiment_id": experiment.id},
            )
            self._record(
                incident_id,
                Stage.experiment,
                EventKind.page_human,
                Actor.orchestrator,
                "experiment refused; page human",
                {},
            )
            return None
        return experiment

    def confirmation_experiment(
        self,
        incident_id: str,
        triage: TriageResult,
        verdict: Verdict,
        previous: Experiment,
    ) -> Experiment | None:
        if not verdict.support:
            return None
        leader = max(verdict.support, key=lambda item: item.support).hypothesis_id
        experiment = self._brain.confirmation_experiment(
            triage,
            leader,
            self._levers.catalog(),
            self._levers.estimate_blast_radius,
            {previous.id},
        )
        if experiment is None:
            return None
        if experiment.blast_radius_pct > 50:
            self._record(
                incident_id,
                Stage.experiment,
                EventKind.refused,
                Actor.orchestrator,
                f"confirmation blast radius {experiment.blast_radius_pct:g}% exceeds 50%",
                {"experiment_id": experiment.id, "blast_radius_pct": experiment.blast_radius_pct},
            )
            return None
        self._renderer.event(
            "plan", f"{experiment.id} selected to confirm {leader}, blast radius {experiment.blast_radius_pct:g}%"
        )
        return experiment

    def investigate(
        self,
        incident_id: str,
        triage: TriageResult,
        production_incident: Fingerprint,
        production_probe: Experiment,
        now: datetime,
    ) -> tuple[list[HypothesisInvestigation], TriageResult | None]:
        """Stage 4a: one investigator per hypothesis, each in a clean clone, before production
        is touched. Evidence is recorded per hypothesis. With ``investigation_gate`` on, a
        hypothesis that fails to reproduce the production fingerprint is dropped; if none
        survives, a human is paged and the production probe is not run.
        """
        if self._investigation is None:
            return [], triage
        self._renderer.event(
            "investigate",
            f"forking production into {len(triage.hypotheses)} clean clones, one per hypothesis",
        )
        healthy = [
            fp
            for fp in self._telemetry.series(now - timedelta(seconds=BASELINE_S), now)
            if not any(slo.breached for slo in fp.slos)
        ]
        try:
            results = self._investigation.investigate(
                incident_id, triage, production_incident, healthy, production_probe
            )
        except Exception as exc:  # noqa: BLE001 - PRD: the clone lab never threatens the v5 loop
            self._record(
                incident_id, Stage.experiment, EventKind.refused, Actor.adapter,
                f"clone investigation unavailable, continuing with the production probe: {exc}",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            self._renderer.event("investigate", f"unavailable ({type(exc).__name__}) — continuing without clones")
            return [], triage
        for item in results:
            self._record(
                incident_id,
                Stage.experiment,
                EventKind.triage,
                Actor.math,
                f"clone investigation {item.hypothesis_id}: {item.detail}",
                {
                    "investigation": True,
                    "hypothesis_id": item.hypothesis_id,
                    "clone_id": item.clone_id,
                    "recipe": item.recipe,
                    "reproduced": item.reproduced,
                    "recovered": item.recovered,
                    "prediction_matches": item.prediction_matches,
                    "prediction_total": item.prediction_total,
                    "survives": item.survives,
                    "evidence": item.evidence,
                    "attempts": item.attempts,
                },
                experiment_id=production_probe.id,
            )
            verdict = "survives" if item.survives else "falsified"
            self._renderer.event("investigate", f"{item.hypothesis_id} {verdict}: {item.detail}")
        if not self._investigation_gate:
            return results, triage
        survivors = {item.hypothesis_id for item in results if item.reproduced}
        if not survivors:
            self._record(
                incident_id, Stage.experiment, EventKind.page_human, Actor.orchestrator,
                "no hypothesis reproduced the incident in a clone; page human",
                {"hypotheses": [h.id for h in triage.hypotheses]},
            )
            self._renderer.event("investigate", "nothing reproduced — paged human")
            return results, None
        dropped = [h.id for h in triage.hypotheses if h.id not in survivors]
        if dropped:
            self._record(
                incident_id, Stage.experiment, EventKind.refused, Actor.math,
                f"dropped before production: {', '.join(dropped)} did not reproduce in a clone",
                {"dropped": dropped, "survivors": sorted(survivors)},
            )
            triage = triage.model_copy(
                update={
                    "hypotheses": [h for h in triage.hypotheses if h.id in survivors],
                    "predictions": [p for p in triage.predictions if p.hypothesis_id in survivors],
                }
            )
        return results, triage

    def experiment(
        self, incident_id: str, experiment: Experiment, now: datetime
    ) -> tuple[list[Fingerprint], list[Fingerprint], list[Fingerprint]]:
        baseline = self._telemetry.series(now - timedelta(seconds=BASELINE_S), now)
        spec = next(spec for spec in self._levers.catalog() if spec.id == experiment.lever_id)
        action = self._apply(
            incident_id,
            experiment.lever_id,
            experiment.params,
            experiment.hold_s + 30,
            Stage.experiment,
            experiment.id,
        )
        self._record(
            incident_id,
            Stage.experiment,
            EventKind.experiment_start,
            Actor.orchestrator,
            f"{experiment.lever_id} experiment started",
            {"hold_s": experiment.hold_s, "ttl_s": experiment.hold_s + 30},
            action_id=action.action_id,
            experiment_id=experiment.id,
        )
        self._record(
            incident_id,
            Stage.experiment,
            EventKind.action_apply,
            Actor.adapter,
            f"applied {experiment.lever_id}",
            action.model_dump(mode="json"),
            action_id=action.action_id,
            experiment_id=experiment.id,
        )
        self._renderer.event("experiment", f"{experiment.lever_id} on: {experiment.params}")
        self._sleep(experiment.hold_s)
        t = self._clock()
        during = self._telemetry.series(t - timedelta(seconds=experiment.hold_s), t)
        released = self._levers.undo(action)
        self._record(
            incident_id,
            Stage.experiment,
            EventKind.action_undo,
            Actor.adapter,
            f"released {experiment.lever_id}",
            released.model_dump(mode="json"),
            action_id=action.action_id,
            experiment_id=experiment.id,
        )
        self._record(
            incident_id,
            Stage.experiment,
            EventKind.experiment_end,
            Actor.orchestrator,
            "lever released; after-release observation started",
            {"status": released.status.value},
            action_id=action.action_id,
            experiment_id=experiment.id,
        )
        self._renderer.event("experiment", f"{experiment.lever_id} released")
        self._sleep(spec.default_watch_s)
        after_release = self._telemetry.series(t, self._clock())
        self._renderer.event(
            "observe", f"after-release window collected ({len(after_release)} windows)"
        )
        return baseline, during, after_release

    def judge(
        self,
        incident_id: str,
        triage: TriageResult,
        experiment: Experiment,
        baseline: list[Fingerprint],
        during: list[Fingerprint],
        after_release: list[Fingerprint],
    ) -> Verdict:
        verdict = self._brain.judge(triage, experiment, baseline, during, after_release)
        self._record(
            incident_id,
            Stage.mitigate,
            EventKind.verdict,
            Actor.math,
            verdict.summary,
            {
                "diagnosis": verdict.diagnosis,
                "confirmed": verdict.confirmed,
                "observations": [item.model_dump(mode="json") for item in verdict.observations],
            },
            experiment_id=experiment.id,
        )
        self._renderer.event(
            "judge", f"{verdict.diagnosis} {'confirmed' if verdict.confirmed else 'not confirmed'}"
        )
        return verdict

    def mitigate(
        self, incident_id: str, triage: TriageResult, experiment: Experiment, verdict: Verdict
    ) -> ActionHandle | None:
        confirming = next(
            (
                p
                for p in triage.predictions
                if p.hypothesis_id == verdict.diagnosis and p.experiment_id == experiment.id
            ),
            None,
        )
        if (
            confirming
            and confirming.confirms_if is not None
            and confirming.confirms_if.phase.value == "after_release"
            and confirming.confirms_if.expect.value == "within_baseline"
        ):
            spec = next(spec for spec in self._levers.catalog() if spec.id == experiment.lever_id)
            action = self._apply(
                incident_id,
                experiment.lever_id,
                experiment.params,
                spec.max_ttl_s,
                Stage.mitigate,
                experiment.id,
            )
            self._record(
                incident_id,
                Stage.mitigate,
                EventKind.action_apply,
                Actor.adapter,
                f"re-applied {experiment.lever_id} as mitigation",
                action.model_dump(mode="json"),
                action_id=action.action_id,
                experiment_id=experiment.id,
            )
            self._record(
                incident_id,
                Stage.mitigate,
                EventKind.mitigation,
                Actor.orchestrator,
                "experiment lever kept as durable mitigation",
                {"lever_id": experiment.lever_id, "action_id": action.action_id},
                action_id=action.action_id,
                experiment_id=experiment.id,
            )
            return action
        # The probe was diagnostic, not curative (H_db: the cap only lowered load). Hold the
        # lever that directly relieves the diagnosed cause instead, so production is not left in
        # the incident while the durable fix is written and verified.
        relief = self._relief_experiment(triage, verdict.diagnosis, experiment)
        if relief is not None:
            spec = next(spec for spec in self._levers.catalog() if spec.id == relief.lever_id)
            try:
                action = self._apply(
                    incident_id, relief.lever_id, relief.params, spec.max_ttl_s, Stage.mitigate, relief.id
                )
            except (LeverError, BudgetExceeded) as exc:
                self._record(
                    incident_id, Stage.mitigate, EventKind.refused, Actor.adapter,
                    f"relief lever {relief.lever_id} unavailable: {exc}", {"lever_id": relief.lever_id},
                )
                action = None
            if action is not None:
                self._record(
                    incident_id, Stage.mitigate, EventKind.action_apply, Actor.adapter,
                    f"applied {relief.lever_id} as mitigation", action.model_dump(mode="json"),
                    action_id=action.action_id, experiment_id=relief.id,
                )
                self._record(
                    incident_id, Stage.mitigate, EventKind.mitigation, Actor.orchestrator,
                    f"{relief.lever_id} held as mitigation while the durable fix is prepared",
                    {"lever_id": relief.lever_id, "action_id": action.action_id, "ttl_s": spec.max_ttl_s},
                    action_id=action.action_id, experiment_id=relief.id,
                )
                self._renderer.event("mitigate", f"{relief.lever_id} held (ttl {spec.max_ttl_s}s)")
                return action
        self._record(
            incident_id,
            Stage.mitigate,
            EventKind.mitigation,
            Actor.orchestrator,
            "experiment lever released; durable fix required",
            {"lever_id": experiment.lever_id},
            experiment_id=experiment.id,
        )
        return None

    def _relief_experiment(
        self, triage: TriageResult, diagnosis: str, probe: Experiment
    ) -> Experiment | None:
        """The catalog lever that directly relieves the diagnosed cause (Brain's confirmation
        experiment for that hypothesis), if it is not the probe we already ran."""
        find = getattr(self._brain, "confirmation_experiment", None)
        if find is None:
            return None
        relief = find(triage, diagnosis, self._levers.catalog(), self._levers.estimate_blast_radius, set())
        if relief is None or relief.blast_radius_pct > 50:
            return None
        return relief  # may be the probe itself (e.g. failover confirmed H_db): then it is re-held

    def patch(self, incident_id: str, verdict: Verdict, triage: TriageResult) -> PatchProposal:
        patch = self._patches.propose(incident_id, verdict, triage)
        self._record(
            incident_id,
            Stage.patch,
            EventKind.patch_opened,
            Actor.adapter,
            patch.summary,
            {
                "provider": patch.provider, "reference": patch.reference,
                "revision": patch.revision, "session_id": patch.session_id,
            },
        )
        self._renderer.event("patch", f"{patch.provider} patch prepared")
        return patch

    def ship(
        self,
        incident_id: str,
        patch: PatchProposal,
        diagnosis: str,
        mitigation: ActionHandle | None,
    ) -> tuple[PatchProposal, PatchVerification, CanaryResult]:
        """Stages 6b-7: checkout -> clone verification -> production canary, with measured
        evidence sent back to the patch author for at most ``max_revisions`` revisions.

        The mitigation is released only once, before the first canary attempt.
        """
        revisions = 0
        while True:
            context = self.checkout(incident_id, patch)
            verification = self.verify_patch(incident_id, patch, diagnosis, context)
            if verification.status == VerificationStatus.failed:
                canary = self._refuse_canary(
                    incident_id, f"patch failed clone verification: {verification.detail}"
                )
                evidence = _verification_evidence(verification)
            else:
                canary = self.canary(incident_id, patch, mitigation, context)
                mitigation = None
                if canary.status != CanaryStatus.regressed:
                    return patch, verification, canary
                evidence = f"production canary regressed: {canary.detail}"
            if revisions >= self._max_revisions:
                return patch, verification, canary
            revised = self.revise(incident_id, patch, evidence)
            if revised is None:
                return patch, verification, canary
            patch, revisions = revised, revisions + 1

    def checkout(self, incident_id: str, patch: PatchProposal) -> Path | None:
        if self._checkout is None:
            return None
        try:
            context = self._checkout.resolve(patch)
        except CanaryPreparationError as exc:
            self._record(
                incident_id, Stage.patch, EventKind.refused, Actor.adapter,
                f"could not check out {patch.reference}: {exc}", {"patch_reference": patch.reference},
            )
            self._renderer.event("patch", f"checkout failed: {exc}")
            return None
        if context is not None:
            self._renderer.event("patch", f"checked out {patch.reference} -> {context}")
        return context

    def revise(self, incident_id: str, patch: PatchProposal, evidence: str) -> PatchProposal | None:
        """Send the measured failure back to the patch author (the same Devin session)."""
        self._renderer.event("patch", f"sending evidence back to {patch.provider} for a revision")
        revised = self._patches.revise(incident_id, patch, evidence)
        if revised is None:
            self._record(
                incident_id, Stage.patch, EventKind.page_human, Actor.orchestrator,
                f"{patch.provider} patch cannot be revised automatically; page human",
                {"patch_reference": patch.reference, "evidence_text": evidence},
            )
            self._renderer.event("patch", "no revision available — paged human")
            return None
        self._record(
            incident_id, Stage.patch, EventKind.patch_opened, Actor.adapter, revised.summary,
            {
                "provider": revised.provider, "reference": revised.reference,
                "revision": revised.revision, "session_id": revised.session_id,
                "evidence_text": evidence,  # str; `evidence` is reserved for the object form (ES mapping)
            },
        )
        self._renderer.event("patch", f"{revised.provider} revision {revised.revision} received")
        return revised

    def verify_patch(
        self,
        incident_id: str,
        patch: PatchProposal,
        diagnosis: str,
        context: Path | None = None,
    ) -> PatchVerification:
        """Stage 6b (PRD v6): replay the reproduced incident against the patch in a clean clone.

        Runs behind the v5 path: without a lab the result is ``skipped`` and the production
        canary proceeds. A ``failed`` result refuses the canary and pages a human with evidence.
        """
        if self._verifier is None:
            verification = PatchVerification(VerificationStatus.skipped, "no clone lab configured")
        else:
            self._renderer.event("verify", "replaying the reproduced incident against the patch in a clone")
            verification = self._verifier.verify(incident_id, patch, diagnosis, context)
        payload = {
            "status": verification.status.value,
            "clone_id": verification.clone_id,
            "recipe": verification.recipe,
            "evidence": verification.evidence,
            "patch_reference": patch.reference,
        }
        if verification.status == VerificationStatus.failed:
            self._record(
                incident_id, Stage.patch, EventKind.refused, Actor.math,
                f"patch failed clone verification: {verification.detail}", payload,
            )
            self._record(
                incident_id, Stage.patch, EventKind.page_human, Actor.orchestrator,
                "patch did not survive the replayed incident; page human", {},
            )
        else:
            self._record(
                incident_id, Stage.patch, EventKind.canary_update, Actor.math,
                f"clone verification {verification.status.value}: {verification.detail}", payload,
            )
        self._renderer.event("verify", f"{verification.status.value}: {verification.detail}")
        return verification

    def canary(
        self,
        incident_id: str,
        patch: PatchProposal,
        mitigation: ActionHandle | None = None,
        context: Path | None = None,
    ) -> CanaryResult:
        try:
            target = self._canary_deployer.prepare(patch, context)
        except CanaryPreparationError as exc:
            return self._refuse_canary(incident_id, f"patch target unavailable: {exc}")

        if mitigation is not None and mitigation.lever_id in CODE_SUPERSEDES:
            # The patch replaces this lever's job (a bounded retry policy supersedes the retry
            # cap), so judge v2 without it. A relief lever such as db_failover stays: it fixes
            # the dependency, which no Orders patch can, and pulling it would blame the patch
            # for the incident coming back.
            released_mitigation = self._levers.undo(mitigation)
            self._record(
                incident_id,
                Stage.mitigate,
                EventKind.action_undo,
                Actor.adapter,
                "released emergency mitigation before canary verification",
                released_mitigation.model_dump(mode="json"),
                action_id=mitigation.action_id,
            )
        try:
            canary = self._apply(
                incident_id,
                "canary_weight",
                {"v2_weight": 0.05},
                300,
                Stage.canary,
            )
        except LeverError as exc:
            return self._refuse_canary(incident_id, f"canary_weight refused: {exc}", target)
        self._record(
            incident_id,
            Stage.canary,
            EventKind.action_apply,
            Actor.adapter,
            "applied canary_weight",
            {**canary.model_dump(mode="json"), "target": asdict(target)},
            action_id=canary.action_id,
        )
        spec = next(spec for spec in self._levers.catalog() if spec.id == "canary_weight")
        canary_start = self._clock()
        self._sleep(spec.default_watch_s)
        canary_end = self._clock()
        fingerprints = self._telemetry.series(canary_start, canary_end)
        released = self._levers.undo(canary)
        self._record(
            incident_id,
            Stage.canary,
            EventKind.action_undo,
            Actor.adapter,
            "released canary_weight",
            released.model_dump(mode="json"),
            action_id=canary.action_id,
        )
        regression = self._canary_regression(fingerprints, target)
        if regression is not None:
            self._record(
                incident_id,
                Stage.canary,
                EventKind.refused,
                Actor.orchestrator,
                f"canary regression, auto-rolled back: {regression}",
                {"reason": regression, "target": asdict(target)},
                action_id=canary.action_id,
            )
            self._record(
                incident_id,
                Stage.canary,
                EventKind.page_human,
                Actor.orchestrator,
                "canary regression; page human",
                {},
            )
            self._renderer.event("canary", f"regression: {regression} — paged human")
            return CanaryResult(CanaryStatus.regressed, regression, target)
        self._record(
            incident_id,
            Stage.canary,
            EventKind.canary_update,
            Actor.orchestrator,
            "5% canary verified",
            {"v2_weight": 0.05, "target": asdict(target)},
            action_id=canary.action_id,
        )
        self._renderer.event("canary", "canary_weight v2=0.05 verified and released")
        return CanaryResult(CanaryStatus.passed, "5% canary verified", target)

    def _refuse_canary(
        self,
        incident_id: str,
        detail: str,
        target: CanaryTarget | None = None,
    ) -> CanaryResult:
        payload = {"lever_id": "canary_weight", "params": {"v2_weight": 0.05}}
        if target is not None:
            payload["target"] = asdict(target)
        self._record(
            incident_id,
            Stage.canary,
            EventKind.refused,
            Actor.orchestrator,
            detail,
            payload,
        )
        self._record(
            incident_id,
            Stage.canary,
            EventKind.page_human,
            Actor.orchestrator,
            "canary unavailable; page human",
            {},
        )
        self._renderer.event("canary", f"{detail} — paged human")
        return CanaryResult(CanaryStatus.refused, detail, target)

    @staticmethod
    def _canary_regression(
        fingerprints: list[Fingerprint], target: CanaryTarget
    ) -> str | None:
        if not fingerprints:
            return "no telemetry collected during canary"
        if any(slo.breached for fp in fingerprints for slo in fp.slos):
            return "checkout SLO breached"
        if target.service_name is None:
            return None
        pairs = [
            (fp.services.get("orders"), fp.services.get(target.service_name), fp)
            for fp in fingerprints
            if fp.services.get("orders") is not None
            and fp.services.get(target.service_name) is not None
        ]
        if not pairs:
            return f"missing {target.service_name} telemetry"
        if not any(v2.qps for _, v2, _ in pairs):
            return f"{target.service_name} served no traffic during canary"
        # A service that never errored has no error counter yet, so error_rate is honestly
        # None rather than 0; compare error rates only when both versions report them.
        v1_errors = [v1.error_rate for v1, _, _ in pairs if v1.error_rate is not None]
        v2_errors = [v2.error_rate for _, v2, _ in pairs if v2.error_rate is not None]
        if v1_errors and v2_errors and sum(v2_errors) / len(v2_errors) > sum(v1_errors) / len(v1_errors):
            return "orders-v2 error rate exceeds orders-v1"
        thresholds = [slo.threshold for _, _, fp in pairs for slo in fp.slos if slo.name == "checkout"]
        v2_p99 = [v2.p99_ms for _, v2, _ in pairs if v2.p99_ms is not None]
        if not thresholds or not v2_p99:
            return "missing version-specific latency evidence"
        if sum(v2_p99) / len(v2_p99) > min(thresholds):
            return "orders-v2 p99 exceeds checkout threshold"
        return None

    def _apply(
        self,
        incident_id: str,
        lever_id: str,
        params: dict,
        ttl_s: int,
        stage: Stage,
        experiment_id: str | None = None,
    ) -> ActionHandle:
        if self._actions >= self._action_budget:
            self._record(
                incident_id,
                stage,
                EventKind.page_human,
                Actor.orchestrator,
                "action budget exceeded; page human",
                {"action_budget": self._action_budget},
            )
            raise BudgetExceeded("action budget exceeded")
        self._actions += 1
        return self._levers.apply(lever_id, params, ttl_s)

    def _record(
        self,
        incident_id: str,
        stage: Stage,
        kind: EventKind,
        actor: Actor,
        summary: str,
        payload: dict,
        action_id: str | None = None,
        experiment_id: str | None = None,
    ) -> None:
        self._audit.write(
            AuditEvent(
                incident_id=incident_id,
                ts=self._clock(),
                stage=stage,
                kind=kind,
                actor=actor,
                summary=summary,
                payload=payload,
                action_id=action_id,
                experiment_id=experiment_id,
            )
        )


def _verification_evidence(verification: PatchVerification) -> str:
    lines = [f"clone verification failed: {verification.detail}"]
    if verification.recipe:
        lines.append(f"replayed recipe: {json.dumps(verification.recipe)}")
    if verification.evidence:
        lines.append(f"measured: {json.dumps(verification.evidence)}")
    return "\n".join(lines)
