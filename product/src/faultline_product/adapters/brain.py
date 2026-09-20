from collections.abc import Callable
from typing import Any

from faultline_brain import (
    DEFAULT_MODEL,
    SYSTEM_PROMPT,
    NoiseModel,
    TriageValidationError,
    confirmation_experiment,
    judge,
    plan_experiment,
    run_triage,
    score_experiment,
)
from faultline_contracts import (
    Experiment,
    ExperimentWindow,
    Fingerprint,
    LeverSpec,
    TriageResult,
    Verdict,
)
from faultline_brain.agent_builder import AgentBuilderError
from faultline_contracts.triage import NONE_OF_THE_ABOVE

INCIDENT_STEADY_WINDOWS = 6

# Product's additions to Owner 3's triage prompt, learned from the first live OpenAI run
# (live-12): the model predicted each hypothesis only for its favourite experiment, so no
# experiment had two hypotheses to compare and the planner found nothing separating. It also
# invented ids per run, which breaks the clone reproduction recipes keyed by class id.
TAXONOMY = """
Hypothesis ids MUST come from this sustaining-cause taxonomy (PRD v6.1); use the id of the
class you mean, and add a short `label`:
  H_meta   self-sustaining retry storm (metastable): the trigger is gone, retries themselves keep
           the dependency saturated. Breaking the loop (e.g. a retry cap) ends it; after the cap is
           released the system STAYS healthy because there is no longer a backlog to retry.
  H_db     degraded dependency / DB capacity (e.g. a batch job): the dependency is genuinely slow
           at normal load. Reducing load lowers its queue but latency per query stays high, and
           once load returns the incident returns; relieving the dependency (failover) heals it.
  H_cpu    resource exhaustion of a service (CPU, pool, FDs): service p99 high while the DB is fine.
  H_deploy bad deploy or config change preceding the breach.
  H_queue  consumer backlog / queue lag.
  H_hotkey hot key or hot shard dominating dependency time.
  H_cache  cache stampede at a TTL edge.
  H_node   one bad node / noisy neighbour, peers fine.
Propose only classes the fingerprint makes plausible (normally two or three).

Predictions MUST form a full matrix: for EVERY hypothesis, give one prediction for EVERY
candidate experiment id, each with `during` and `after_release` directions on the same
metrics, so the planner can compare hypotheses experiment by experiment. Where a hypothesis
expects no change, say `flat` explicitly. Prefer these metrics when present:
db.query_p50_ms, db.qps, svc.orders.retry_ratio, svc.gateway.p99_ms, svc.gateway.error_rate,
db.pool_busy_ratio.

`confirms_if` must test that the CAUSE was addressed, not that a symptom moved: for a
dependency hypothesis (H_db) require the user-facing SLO metric (svc.gateway.p99_ms) to recover
while the dependency is relieved, never just the dependency's own latency — a DB that is merely
a victim of a saturated caller also gets faster when relieved, and that world must remain
none-of-the-above.
"""
PRODUCT_SYSTEM_PROMPT = SYSTEM_PROMPT + "\n" + TAXONOMY


class LiveBrain:
    """Product-side Brain: OpenAI proposes (run_triage), math decides (plan_experiment, judge)."""

    def __init__(
        self,
        candidates: list[Experiment],
        client: Any | None = None,
        model: str = DEFAULT_MODEL,
        triage_fallback: TriageResult | None = None,
        usage_sink: Callable[[dict], None] | None = None,
        fallback_client: Any | None = None,
        provider: str = "openai",
        provider_sink: Callable[[dict], None] | None = None,
        evidence_reader: Any | None = None,
    ):
        self._candidates = list(candidates)
        self._client = client
        self._model = model
        self._triage_fallback = triage_fallback
        self._usage_sink = usage_sink
        self._fallback_client = fallback_client
        self._provider = provider
        self._provider_sink = provider_sink
        self._evidence_reader = evidence_reader
        self.last_triage_source: str | None = None
        self.last_triage_note: str | None = None

    def triage(self, incident_id: str, fingerprint: Fingerprint) -> TriageResult:
        if self._client is None:
            if self._triage_fallback is None:
                raise RuntimeError("no OpenAI client and no triage fallback")
            self.last_triage_source = "fallback"
            self.last_triage_note = "fallback (no OPENAI_API_KEY)"
            return self._triage_fallback.model_copy(update={"incident_id": incident_id})
        context = None
        if self._provider == "agent_builder" and self._evidence_reader is not None:
            from .evidence import evidence_metadata, production_evidence

            context = production_evidence(self._evidence_reader, incident_id, fingerprint)
            if self._provider_sink:
                self._provider_sink(
                    {
                        "provider": "agent_builder",
                        "role": "triage",
                        "status": "evidence_loaded",
                        **evidence_metadata(context),
                    }
                )
        failures = []
        clients = [(self._client, self._provider)]
        if self._fallback_client is not None:
            clients.append((self._fallback_client, "openai"))
        for client, provider in clients:
            try:
                result = run_triage(
                    client.with_context(context) if provider == "agent_builder" and context is not None else client,
                    fingerprint,
                    self._candidates,
                    incident_id,
                    model=self._model,
                    system_prompt=PRODUCT_SYSTEM_PROMPT,
                    usage_sink=self._usage_sink,
                )
                if provider == "agent_builder" and plan_experiment(result, self._candidates).selected is None:
                    raise TriageValidationError("Agent Builder predictions did not separate hypotheses")
            except Exception as exc:  # noqa: BLE001 - triage must never end the incident
                description = str(exc) if isinstance(exc, AgentBuilderError) else type(exc).__name__
                failures.append(
                    f"{'OpenAI' if provider == 'openai' else provider} error: {description}"
                )
                if self._provider_sink:
                    event = {"provider": provider, "role": "triage", "status": "rejected", "reason": type(exc).__name__}
                    if isinstance(exc, AgentBuilderError):
                        event["error"] = exc.diagnostic()
                    self._provider_sink(event)
                continue
            if (
                self._triage_fallback is not None
                and plan_experiment(result, self._candidates).selected is None
                and plan_experiment(self._triage_fallback, self._candidates).selected is not None
            ):
                # The model proposed, but nothing in its prediction matrix separates its own
                # hypotheses, so no experiment can be planned. Keep the incident moving on the
                # canonical hypotheses and say so; the model's proposal is kept for the report.
                self.last_triage_source = "fallback"
                self.last_triage_note = (
                    "fallback (OpenAI predictions did not separate "
                    + " vs ".join(h.id for h in result.hypotheses)
                    + ")"
                )
                if self._provider_sink:
                    self._provider_sink({"provider": "fixture", "role": "triage", "status": "fallback"})
                return self._triage_fallback.model_copy(update={"incident_id": incident_id})
            self.last_triage_source = provider
            self.last_triage_note = provider + (" (fallback after " + ", ".join(failures) + ")" if failures else "")
            if self._provider_sink:
                self._provider_sink({"provider": provider, "role": "triage", "status": "validated"})
            return result
        if self._triage_fallback is None:
            raise RuntimeError("triage failed: " + ", ".join(failures))
        self.last_triage_source = "fallback"
        self.last_triage_note = "fallback (" + ", ".join(failures) + ")"
        if self._provider_sink:
            self._provider_sink({"provider": "fixture", "role": "triage", "status": "fallback"})
        return self._triage_fallback.model_copy(update={"incident_id": incident_id})

    def triage_source(self) -> str | None:
        return self.last_triage_note

    def _scored_candidates(
        self, catalog: list[LeverSpec], blast_radius: Callable[[str, dict], float]
    ) -> list[Experiment]:
        catalog_ids = {spec.id for spec in catalog}
        return [
            candidate.model_copy(
                update={"blast_radius_pct": blast_radius(candidate.lever_id, candidate.params)}
            )
            for candidate in self._candidates
            if candidate.lever_id in catalog_ids
        ]

    def plan(
        self,
        triage: TriageResult,
        catalog: list[LeverSpec],
        blast_radius: Callable[[str, dict], float],
    ) -> Experiment | None:
        return plan_experiment(triage, self._scored_candidates(catalog, blast_radius)).selected

    def plan_scores(
        self,
        triage: TriageResult,
        catalog: list[LeverSpec],
        blast_radius: Callable[[str, dict], float],
    ) -> list[dict]:
        scores = [
            score_experiment(triage.predictions, candidate)
            for candidate in self._scored_candidates(catalog, blast_radius)
        ]
        scores.sort(
            key=lambda item: (-item.score, -item.separation, item.experiment.blast_radius_pct, item.experiment.id)
        )
        return [
            {
                "experiment_id": item.experiment.id,
                "lever_id": item.experiment.lever_id,
                "separation": item.separation,
                "score": item.score,
                "blast_radius_pct": item.experiment.blast_radius_pct,
            }
            for item in scores
        ]

    def confirmation_experiment(
        self,
        triage: TriageResult,
        hypothesis_id: str,
        catalog: list[LeverSpec],
        blast_radius: Callable[[str, dict], float],
        excluded_ids: set[str],
    ) -> Experiment | None:
        return confirmation_experiment(
            triage, hypothesis_id, self._scored_candidates(catalog, blast_radius), excluded_ids=excluded_ids
        )

    def judge(
        self,
        triage: TriageResult,
        experiment: Experiment,
        baseline: list[Fingerprint],
        during: list[Fingerprint],
        after_release: list[Fingerprint],
    ) -> Verdict:
        if not during or not after_release:
            return Verdict(
                incident_id=triage.incident_id,
                diagnosis=NONE_OF_THE_ABOVE,
                confirmed=False,
                support=[],
                observations=[],
                summary="insufficient telemetry: no during/after-release windows",
            )

        series = {fp.window_start: fp for fp in [*baseline, *during, *after_release]}
        ordered_series = sorted(series.values(), key=lambda fp: fp.window_start)
        experiment_window = ExperimentWindow(
            experiment_id=experiment.id,
            start=during[0].window_start,
            release=after_release[0].window_start,
        )
        healthy, incident = _baselines(baseline)
        if not healthy:
            return Verdict(
                incident_id=triage.incident_id,
                diagnosis=NONE_OF_THE_ABOVE,
                confirmed=False,
                support=[],
                observations=[],
                summary="insufficient telemetry: no healthy baseline windows",
            )
        if not incident:
            return Verdict(
                incident_id=triage.incident_id,
                diagnosis=NONE_OF_THE_ABOVE,
                confirmed=False,
                support=[],
                observations=[],
                summary="insufficient telemetry: no breached incident baseline windows",
            )
        return judge(
            triage,
            ordered_series,
            [experiment_window],
            NoiseModel.from_windows(healthy),
            NoiseModel.from_windows(incident),
        )


def _baselines(baseline: list[Fingerprint]) -> tuple[list[Fingerprint], list[Fingerprint]]:
    """Split C1 history into true healthy windows and a steady incident tail.

    The breach ignition ramp is not measurement noise. Including it in the
    incident standard deviation lets a large experiment response appear flat,
    so the judge receives only the latest six breached windows (30 seconds).
    Healthy evidence is never substituted with incident telemetry: without it,
    an after-release ``within_baseline`` confirmation is undefined.
    """
    healthy = [fp for fp in baseline if not any(slo.breached for slo in fp.slos)]
    breached = [fp for fp in baseline if any(slo.breached for slo in fp.slos)]
    return healthy, breached[-INCIDENT_STEADY_WINDOWS:]


def build_live_brain(
    candidates: list[Experiment],
    *,
    api_key: str | None,
    model: str,
    triage_fallback: TriageResult | None,
    proposal_client: Any | None = None,
    provider_sink: Callable[[dict], None] | None = None,
    evidence_reader: Any | None = None,
) -> LiveBrain:
    client = None
    if api_key:
        try:
            from openai import OpenAI
        except ImportError as exc:
            if proposal_client is None:
                raise RuntimeError("install with: uv sync --extra llm") from exc
            if provider_sink:
                provider_sink({"provider": "openai", "role": "triage", "status": "unavailable", "reason": "ImportError"})
        else:
            client = OpenAI(api_key=api_key)
    if proposal_client is not None:
        return LiveBrain(
            candidates,
            client=proposal_client,
            fallback_client=client,
            provider="agent_builder",
            model=model,
            triage_fallback=triage_fallback,
            provider_sink=provider_sink,
            evidence_reader=evidence_reader,
        )
    return LiveBrain(
        candidates,
        client=client,
        model=model,
        triage_fallback=triage_fallback,
    )
