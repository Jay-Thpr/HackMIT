import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

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

from .ports import Brain, PatchAdapter, PatchProposal
from .renderer import TerminalRenderer

BASELINE_S = 120


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    incident_id: str
    diagnosis: str
    patch: PatchProposal | None


class Orchestrator:
    def __init__(
        self,
        levers: LeverAdapter,
        audit: AuditSink,
        patches: PatchAdapter,
        renderer: TerminalRenderer,
        telemetry: TelemetrySource,
        brain: Brain,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        action_budget: int = 5,
    ):
        self._levers, self._audit, self._patches = levers, audit, patches
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
        baseline, during, after_release = self.experiment(incident_id, experiment, now)
        verdict = self.judge(incident_id, triage, experiment, baseline, during, after_release)
        if verdict.diagnosis == NONE_OF_THE_ABOVE or not verdict.confirmed:
            self._record(
                incident_id,
                Stage.mitigate,
                EventKind.page_human,
                Actor.orchestrator,
                "diagnosis not confirmed",
                {},
            )
            return RunResult(incident_id, verdict.diagnosis, None)
        self.mitigate(incident_id, triage, experiment, verdict)
        patch = self.patch(incident_id, verdict, triage)
        self.canary(incident_id, patch)
        self._record(
            incident_id,
            Stage.report,
            EventKind.report,
            Actor.orchestrator,
            "incident report ready",
            {"diagnosis": verdict.diagnosis, "patch_reference": patch.reference},
        )
        self._renderer.event("report", f"ready: faultline report --incident {incident_id}")
        return RunResult(incident_id, verdict.diagnosis, patch)

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
    ) -> None:
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
        else:
            self._record(
                incident_id,
                Stage.mitigate,
                EventKind.mitigation,
                Actor.orchestrator,
                "experiment lever released; durable fix required",
                {"lever_id": experiment.lever_id},
                experiment_id=experiment.id,
            )

    def patch(self, incident_id: str, verdict: Verdict, triage: TriageResult) -> PatchProposal:
        patch = self._patches.propose(incident_id, verdict, triage)
        self._record(
            incident_id,
            Stage.patch,
            EventKind.patch_opened,
            Actor.adapter,
            patch.summary,
            {"provider": patch.provider, "reference": patch.reference},
        )
        self._renderer.event("patch", f"{patch.provider} patch prepared")
        return patch

    def canary(self, incident_id: str, patch: PatchProposal) -> None:
        del patch
        try:
            canary = self._apply(
                incident_id,
                "canary_weight",
                {"v2_weight": 0.05},
                300,
                Stage.canary,
            )
        except LeverError as exc:
            self._record(
                incident_id,
                Stage.canary,
                EventKind.refused,
                Actor.orchestrator,
                f"canary_weight refused: {exc}",
                {"lever_id": "canary_weight", "params": {"v2_weight": 0.05}},
            )
            self._record(
                incident_id,
                Stage.canary,
                EventKind.page_human,
                Actor.orchestrator,
                "canary unavailable; page human",
                {},
            )
            self._renderer.event("canary", f"canary_weight refused: {exc} — paged human")
            return
        self._record(
            incident_id,
            Stage.canary,
            EventKind.action_apply,
            Actor.adapter,
            "applied canary_weight",
            canary.model_dump(mode="json"),
            action_id=canary.action_id,
        )
        spec = next(spec for spec in self._levers.catalog() if spec.id == "canary_weight")
        self._sleep(spec.default_watch_s)
        fp = self._telemetry.window(self._clock() - timedelta(seconds=WINDOW_S), self._clock())
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
        if any(slo.breached for slo in fp.slos):
            self._record(
                incident_id,
                Stage.canary,
                EventKind.refused,
                Actor.orchestrator,
                "canary regression, auto-rolled back",
                {},
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
            return
        self._record(
            incident_id,
            Stage.canary,
            EventKind.canary_update,
            Actor.orchestrator,
            "5% canary verified",
            {"v2_weight": 0.05},
            action_id=canary.action_id,
        )
        self._renderer.event("canary", "canary_weight v2=0.05 verified and released")

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
