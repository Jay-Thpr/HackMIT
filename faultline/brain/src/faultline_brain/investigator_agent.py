"""LLM-driven clone investigator: an agent proposes C6 lab actions, measurement decides.

``InvestigatorAgent`` owns exactly one hypothesis and proposes the single lab action
that should recreate the production fingerprint in a clean clone, plus the metric
directions its hypothesis predicts. ``AgenticCloneInvestigator`` runs the propose ->
inject -> measure -> reset loop for up to ``budget`` attempts. ``SeedInvestigator``
is a deterministic stand-in for environments without an API key.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from faultline_contracts import CloneInfo, CloneLab, CloneSpec, CloneStatus, Fingerprint, LabActionHandle
from faultline_contracts.clone import LAB_CATALOG, LabActionSpec
from faultline_contracts.openai_schema import strict_response_format
from faultline_contracts.triage import Direction, TriageResult
from faultline_contracts.levers import Experiment

from .investigator import (
    DEFAULT_MATCH_Z,
    KEY_METRICS,
    CloneProbe,
    InvestigationEvidence,
    LabExperiment,
    PredictionEvidence,
    RecoveryEvidence,
    ReproductionEvidence,
    SimilarityEvidence,
    floored_sigma,
    score_clone_prediction,
    similarity,
)
from .noise import NoiseModel
from .telemetry import assert_no_leak
from .triage import _content, _usage

DEFAULT_AGENT_MODEL = "gpt-4.1"

SYSTEM_PROMPT = """You are one of Faultline's clone investigators. You own exactly one hypothesis
about what sustains a production incident. You are given a clean, healthy clone that
exposes only the lab actions listed. Choose the single lab action (with params and
ttl_s) whose injection your hypothesis says should recreate the production
fingerprint in the clone, and predict the direction of each key metric relative to
the healthy baseline once the injected condition is sustaining itself. For a
transient trigger set observe_after_s > ttl_s (the incident must outlast the
trigger); for a persistent cause set observe_after_s around 15. Learn from the
history: if a previous attempt did not reproduce, change the mechanism or magnitude
your hypothesis would predict, never repeat identical params, and prefer switching
to a different action over re-parameterizing one that already failed. Prefer the
smallest intervention (params and ttl_s) your hypothesis predicts will recreate the
fingerprint; an oversized trigger creates damage your hypothesis did not predict.
Set stop=true only
when no catalog action can express this hypothesis. Use only the supplied metric
keys and action ids. You do not know the true cause; measurement decides."""


class InvestigatorValidationError(ValueError):
    """The agent's proposal was malformed or failed lab-catalog validation."""


class LabParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extra_ms: int | None
    capacity_qps: float | None
    service: str | None
    cpus: float | None
    max_retries: int | None
    timeout_ms: int | None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class MetricDirection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    direction: Literal["up", "down", "flat"]


class LabProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    params: LabParams
    ttl_s: int
    observe_after_s: int
    predicted: list[MetricDirection]
    rationale: str
    stop: bool
    stop_reason: str

    def recipe(self) -> dict:
        return {"action": self.action, "params": self.params.as_dict(), "ttl_s": self.ttl_s}


@dataclass
class AttemptRecord:
    proposal: LabProposal
    similarity: SimilarityEvidence
    predicted_total: int
    predicted_matched: int
    reproduced: bool


@dataclass
class AgentInvestigationEvidence:
    attempts: list[AttemptRecord]
    evidence: InvestigationEvidence | None
    recipe: dict | None
    stopped_reason: str | None
    clone_id: str | None = None


def _proposal_response_format() -> dict[str, Any]:
    return strict_response_format(LabProposal, "lab_proposal")


def _validate_proposal(
    content: str, catalog: list[LabActionSpec], known_metrics: set[str]
) -> LabProposal:
    try:
        proposal = LabProposal.model_validate_json(content)
    except ValueError as exc:
        raise InvestigatorValidationError(f"response did not match LabProposal: {exc}") from exc
    problems: list[str] = []
    spec = next((s for s in catalog if s.id == proposal.action), None)
    if spec is None:
        problems.append(f"unknown action {proposal.action!r}; catalog ids: {[s.id for s in catalog]}")
    else:
        params = proposal.params.as_dict()
        properties = spec.params_schema.get("properties", {})
        required = spec.params_schema.get("required", [])
        unknown = sorted(set(params) - set(properties))
        missing = sorted(set(required) - set(params))
        if unknown:
            problems.append(f"unknown params for {spec.id}: {unknown} (allowed: {sorted(properties)})")
        if missing:
            problems.append(f"missing required params for {spec.id}: {missing}")
        if not 1 <= proposal.ttl_s <= spec.max_ttl_s:
            problems.append(f"ttl_s {proposal.ttl_s} outside 1..{spec.max_ttl_s} for {spec.id}")
    if not 0 <= proposal.observe_after_s <= 120:
        problems.append(f"observe_after_s {proposal.observe_after_s} outside 0..120")
    bad = [p.metric for p in proposal.predicted if p.metric not in known_metrics]
    if bad:
        problems.append(f"predicted metrics not in known_metrics: {sorted(set(bad))}")
    if not proposal.stop and not proposal.predicted:
        problems.append("predicted must be non-empty unless stop is true")
    if problems:
        raise InvestigatorValidationError("; ".join(problems))
    return proposal


class InvestigatorAgent:
    """Proposes the next lab action for one hypothesis via strict OpenAI output."""

    def __init__(
        self,
        client: Any,
        *,
        model: str = DEFAULT_AGENT_MODEL,
        max_attempts_per_call: int = 2,
        usage_sink: Callable[[dict[str, int | None]], None] | None = None,
    ):
        self._client = client
        self._model = model
        self._max_attempts = max_attempts_per_call
        self._usage_sink = usage_sink

    def propose(
        self,
        hypothesis: Any,
        catalog: list[LabActionSpec],
        production_incident: Fingerprint,
        healthy: list[Fingerprint],
        history: list[AttemptRecord],
        attempts_left: int,
    ) -> LabProposal:
        assert_no_leak(production_incident)
        incident_metrics = {
            k: v for k, v in production_incident.metrics().items() if v is not None
        }
        baseline_model = NoiseModel.from_windows(healthy)
        healthy_baseline = {
            m: baseline_model.baseline(m)
            for m in sorted(KEY_METRICS)
            if baseline_model.baseline(m) is not None
        }
        known_metrics = set(incident_metrics) | set(healthy_baseline)
        user_content = {
            "hypothesis": {
                "id": hypothesis.id,
                "label": getattr(hypothesis, "label", hypothesis.id),
                "description": getattr(hypothesis, "description", ""),
            },
            "catalog": [
                {
                    "id": spec.id,
                    "description": spec.description,
                    "params_schema": spec.params_schema,
                    "max_ttl_s": spec.max_ttl_s,
                }
                for spec in catalog
            ],
            "production_fingerprint_metrics": incident_metrics,
            "healthy_baseline": healthy_baseline,
            "history": [
                {
                    "action": record.proposal.action,
                    "params": record.proposal.params.as_dict(),
                    "ttl_s": record.proposal.ttl_s,
                    "matching_metrics": record.similarity.matching_metrics,
                    "shared_metrics": record.similarity.shared_metrics,
                    "mean_abs_z": record.similarity.mean_abs_z,
                    "predicted_matched": record.predicted_matched,
                    "predicted_total": record.predicted_total,
                    "reproduced": record.reproduced,
                }
                for record in history
            ],
            "attempts_left": attempts_left,
            "known_metrics": sorted(known_metrics),
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_content)},
        ]
        last_error: InvestigatorValidationError | None = None
        for attempt in range(max(1, self._max_attempts)):
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format=_proposal_response_format(),
            )
            if self._usage_sink is not None:
                self._usage_sink(_usage(response))
            try:
                return _validate_proposal(_content(response), catalog, known_metrics)
            except InvestigatorValidationError as exc:
                last_error = exc
                if attempt + 1 == self._max_attempts:
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": f"Your previous response failed validation: {exc}. Return a corrected complete JSON object.",
                    }
                )
        raise last_error or InvestigatorValidationError("proposal failed without a validation error")


def _predicted(*items: tuple[str, str]) -> list[MetricDirection]:
    return [MetricDirection(metric=m, direction=d) for m, d in items]


_SEED: dict[str, tuple[dict, int, int, list[MetricDirection]]] = {
    "H_meta": (
        {"action": "db_latency", "params": {"extra_ms": 800}}, 20, 35,
        _predicted(
            ("db.qps", "up"),
            ("db.query_p50_ms", "up"),
            ("svc.orders.retry_ratio", "up"),
            ("svc.orders.error_rate", "up"),
        ),
    ),
    "H_db": (
        {"action": "db_capacity", "params": {"capacity_qps": 40}}, 120, 15,
        _predicted(
            ("db.qps", "up"),
            ("db.query_p50_ms", "up"),
            ("svc.orders.retry_ratio", "up"),
            ("svc.orders.error_rate", "up"),
        ),
    ),
}
_SEED_CPU = (
    {"action": "cpu_limit", "params": {"service": "payments", "cpus": 0.1}}, 120, 15,
    _predicted(("svc.orders.p99_ms", "up"), ("svc.orders.error_rate", "up")),
)


class SeedInvestigator:
    """Deterministic proposal table, used when no OPENAI_API_KEY is configured."""

    def __init__(self):
        self._seen: set[str] = set()

    def propose(
        self,
        hypothesis: Any,
        catalog: list[LabActionSpec],
        production_incident: Fingerprint,
        healthy: list[Fingerprint],
        history: list[AttemptRecord],
        attempts_left: int,
    ) -> LabProposal:
        hid = hypothesis.id
        if hid in self._seen:
            return LabProposal(
                action="db_latency", params=LabParams(extra_ms=1, capacity_qps=None, service=None, cpus=None, max_retries=None, timeout_ms=None),
                ttl_s=1, observe_after_s=0, predicted=[], rationale="",
                stop=True, stop_reason="seed table has one proposal per hypothesis",
            )
        self._seen.add(hid)
        entry = _SEED.get(hid) or (_SEED_CPU if "cpu" in hid.lower() else None)
        if entry is None:
            return LabProposal(
                action="db_latency", params=LabParams(extra_ms=1, capacity_qps=None, service=None, cpus=None, max_retries=None, timeout_ms=None),
                ttl_s=1, observe_after_s=0, predicted=[], rationale="",
                stop=True, stop_reason=f"no seed proposal for {hid}",
            )
        recipe, ttl_s, observe_after_s, predicted = entry
        params = LabParams(
            extra_ms=recipe["params"].get("extra_ms"),
            capacity_qps=recipe["params"].get("capacity_qps"),
            service=recipe["params"].get("service"),
            cpus=recipe["params"].get("cpus"),
            max_retries=recipe["params"].get("max_retries"),
            timeout_ms=recipe["params"].get("timeout_ms"),
        )
        return LabProposal(
            action=recipe["action"], params=params, ttl_s=ttl_s,
            observe_after_s=observe_after_s, predicted=predicted,
            rationale=f"seed recipe for {hid}", stop=False, stop_reason="",
        )


def _measured_direction(model: NoiseModel, metric: str, measured: float) -> Direction:
    sigma = floored_sigma(model, metric)
    baseline = model.baseline(metric)
    if sigma is None or baseline is None:
        raise KeyError(metric)
    if sigma == 0:
        z = float("inf") if measured != baseline else 0.0
    else:
        z = (measured - baseline) / sigma
    if abs(z) < DEFAULT_MATCH_Z:
        return Direction.flat
    return Direction.up if z >= DEFAULT_MATCH_Z else Direction.down


class AgenticCloneInvestigator:
    """Propose -> inject -> measure -> reset loop for one hypothesis.

    Same evidence rules as ``CloneInvestigator`` (reproduce, recover, predict) but the
    action comes from an agent that sees the catalog, the production fingerprint, the
    healthy baseline and its own attempt history.
    """

    def __init__(
        self,
        lab: CloneLab,
        observe: Callable[[CloneInfo], Fingerprint],
        agent: Any,
        *,
        budget: int = 3,
        wait: Callable[[float], None] | None = None,
        catalog: list[LabActionSpec] = LAB_CATALOG,
        agent_for_clone: Callable[[CloneInfo], Any] | None = None,
    ):
        self._lab = lab
        self._observe = observe
        self._agent = agent
        self._agent_for_clone = agent_for_clone
        self._budget = budget
        self._wait = wait or (lambda _seconds: None)
        self._catalog = catalog

    def investigate(
        self,
        hypothesis: Any,
        spec: CloneSpec,
        production_incident: Fingerprint | list[Fingerprint],
        healthy_reference: list[Fingerprint],
        *,
        triage: TriageResult | None = None,
        production_probe: Experiment | None = None,
        run_probe: Callable[[CloneInfo, Experiment, dict], CloneProbe] | None = None,
    ) -> AgentInvestigationEvidence:
        clone = self._lab.create(spec)
        if clone.status != CloneStatus.ready or clone.endpoints is None:
            raise RuntimeError(f"clone {clone.clone_id} was not ready for investigation")
        healthy_model = NoiseModel.from_windows(healthy_reference)
        attempts: list[AttemptRecord] = []
        evidence: InvestigationEvidence | None = None
        recipe: dict | None = None
        stopped_reason: str | None = None
        handle: LabActionHandle | None = None
        try:
            agent = self._agent_for_clone(clone) if self._agent_for_clone else self._agent
            for _attempt in range(self._budget):
                proposal = agent.propose(
                    hypothesis,
                    self._catalog,
                    production_incident if isinstance(production_incident, Fingerprint) else production_incident[-1],
                    healthy_reference,
                    attempts,
                    self._budget - len(attempts),
                )
                if proposal.stop:
                    stopped_reason = proposal.stop_reason or "agent stopped"
                    break
                recipe_candidate = proposal.recipe()
                handle = self._lab.apply(
                    clone.clone_id, proposal.action, proposal.params.as_dict(), proposal.ttl_s
                )
                self._wait(proposal.observe_after_s)
                observed = self._observe(clone)
                sim = similarity(production_incident, observed)
                metrics = observed.metrics()
                predicted_total = predicted_matched = 0
                for item in proposal.predicted:
                    value = metrics.get(item.metric)
                    if value is None or healthy_model.baseline(item.metric) is None:
                        continue
                    predicted_total += 1
                    measured = _measured_direction(healthy_model, item.metric, value)
                    if measured.value == item.direction:
                        predicted_matched += 1
                attempts.append(
                    AttemptRecord(proposal, sim, predicted_total, predicted_matched, sim.matches)
                )
                if not sim.matches:
                    self._lab.undo(handle)
                    handle = None
                    self._lab.reset(clone.clone_id)  # fresh healthy state for the next attempt
                    continue
                experiment = LabExperiment(
                    proposal.action, proposal.params.as_dict(), proposal.ttl_s,
                    observe_after_s=proposal.observe_after_s,
                )
                reproduction = ReproductionEvidence(
                    hypothesis.id, clone.clone_id, experiment, sim, sim.matches
                )
                self._lab.undo(handle)
                handle = None
                # an ignited metastable failure needs time to drain after the cause is gone;
                # keep sampling for ~2 minutes before calling it unrecovered
                self._wait(max(proposal.observe_after_s, 45))
                recovery = similarity(healthy_reference, self._observe(clone))
                for _ in range(3):
                    if recovery.matches:
                        break
                    self._wait(30)
                    recovery = similarity(healthy_reference, self._observe(clone))
                if not recovery.matches:
                    # a self-sustaining failure outlives its trigger by definition; recovery
                    # then means the lab's verified-healthy reset can still bring it back
                    self._lab.reset(clone.clone_id)
                    recovery = similarity(healthy_reference, self._observe(clone))
                recovery_evidence = RecoveryEvidence(clone.clone_id, recovery, recovery.matches)
                probe_evidence: PredictionEvidence | None = None
                if triage is not None or production_probe is not None or run_probe is not None:
                    if triage is None or production_probe is None or run_probe is None:
                        raise ValueError("triage, production_probe, and run_probe must be supplied together")
                    probe_evidence = score_clone_prediction(
                        triage,
                        hypothesis.id,
                        production_probe,
                        run_probe(clone, production_probe, recipe_candidate),
                    )
                evidence = InvestigationEvidence(reproduction, recovery_evidence, probe_evidence)
                recipe = recipe_candidate
                break
            return AgentInvestigationEvidence(attempts, evidence, recipe, stopped_reason, clone.clone_id)
        finally:
            if handle is not None:
                self._lab.undo(handle)
            self._lab.reset(clone.clone_id)
            self._lab.destroy(clone.clone_id)
