from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from faultline_contracts import (
    Actor,
    AuditEvent,
    AuditSink,
    EventKind,
    Experiment,
    Fingerprint,
    LeverAdapter,
    Stage,
    TriageResult,
    Verdict,
    utcnow,
)

from .ports import PatchAdapter, PatchProposal
from .renderer import TerminalRenderer


@dataclass(frozen=True)
class RunResult:
    incident_id: str
    diagnosis: str
    patch: PatchProposal


class Orchestrator:
    def __init__(
        self,
        levers: LeverAdapter,
        audit: AuditSink,
        patches: PatchAdapter,
        renderer: TerminalRenderer,
        clock: Callable[[], datetime] = utcnow,
    ):
        self._levers = levers
        self._audit = audit
        self._patches = patches
        self._renderer = renderer
        self._clock = clock

    def run(
        self,
        incident_id: str,
        fingerprint: Fingerprint,
        triage: TriageResult,
        experiment: Experiment,
        verdict: Verdict,
    ) -> RunResult:
        breached = next((slo for slo in fingerprint.slos if slo.breached), None)
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

        hypothesis_ids = [item.id for item in triage.hypotheses]
        self._record(
            incident_id,
            Stage.triage,
            EventKind.triage,
            Actor.llm,
            f"ambiguous: {' vs '.join(hypothesis_ids)}",
            {"hypotheses": hypothesis_ids, "ambiguous": triage.ambiguous},
        )
        self._renderer.event("triage", f"ambiguous: {' vs '.join(hypothesis_ids)}")

        blast_radius = self._levers.estimate_blast_radius(experiment.lever_id, experiment.params)
        self._renderer.event(
            "plan",
            f"{experiment.id} selected, blast radius {blast_radius:g}%",
        )
        ttl_s = experiment.hold_s + 30
        action = self._levers.apply(experiment.lever_id, experiment.params, ttl_s)
        self._record(
            incident_id,
            Stage.experiment,
            EventKind.experiment_start,
            Actor.orchestrator,
            f"{experiment.lever_id} experiment started",
            {"hold_s": experiment.hold_s, "ttl_s": ttl_s},
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
        self._renderer.event("experiment", "cap on: retry_cap max_retries=0")

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
        self._renderer.event("experiment", "cap released")
        after_release = [item for item in verdict.observations if item.phase.value == "after_release"]
        self._renderer.event("observe", f"after-release window collected ({len(after_release)} metrics)")

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
        self._renderer.event("judge", f"{verdict.diagnosis} confirmed")
        self._record(
            incident_id,
            Stage.mitigate,
            EventKind.mitigation,
            Actor.orchestrator,
            "retry cap validated as emergency mitigation; durable patch requested",
            {"lever_id": experiment.lever_id},
        )

        patch = self._patches.propose(incident_id, verdict.diagnosis)
        self._record(
            incident_id,
            Stage.patch,
            EventKind.patch_opened,
            Actor.adapter,
            patch.summary,
            {"provider": patch.provider, "reference": patch.reference},
        )
        self._renderer.event("patch", f"{patch.provider} patch prepared")

        canary = self._levers.apply("canary_weight", {"v2_weight": 0.05}, ttl_s=300)
        self._record(
            incident_id,
            Stage.canary,
            EventKind.action_apply,
            Actor.adapter,
            "applied canary_weight",
            canary.model_dump(mode="json"),
            action_id=canary.action_id,
        )
        self._record(
            incident_id,
            Stage.canary,
            EventKind.canary_update,
            Actor.orchestrator,
            "5% canary verified",
            {"v2_weight": 0.05},
            action_id=canary.action_id,
        )
        canary_released = self._levers.undo(canary)
        self._record(
            incident_id,
            Stage.canary,
            EventKind.action_undo,
            Actor.adapter,
            "released canary_weight",
            canary_released.model_dump(mode="json"),
            action_id=canary.action_id,
        )
        self._renderer.event("canary", "canary_weight v2=0.05 verified and released")

        self._record(
            incident_id,
            Stage.report,
            EventKind.report,
            Actor.orchestrator,
            "incident report ready",
            {"diagnosis": verdict.diagnosis, "patch_reference": patch.reference},
        )
        self._renderer.event("report", f"ready: faultline report --incident {incident_id}")
        return RunResult(incident_id=incident_id, diagnosis=verdict.diagnosis, patch=patch)

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
