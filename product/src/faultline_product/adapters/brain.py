from collections.abc import Callable
from typing import Any, Literal

from faultline_brain import (
    DEFAULT_MODEL,
    NoiseModel,
    judge,
    plan_experiment,
    run_triage,
)
from faultline_contracts import (
    Experiment,
    ExperimentWindow,
    Fingerprint,
    LeverSpec,
    TriageResult,
    Verdict,
)
from faultline_contracts.triage import NONE_OF_THE_ABOVE


class LiveBrain:
    """Product-side Brain: OpenAI proposes (run_triage), math decides (plan_experiment, judge)."""

    def __init__(
        self,
        candidates: list[Experiment],
        client: Any | None = None,
        model: str = DEFAULT_MODEL,
        triage_fallback: TriageResult | None = None,
        usage_sink: Callable[[dict], None] | None = None,
    ):
        self._candidates = list(candidates)
        self._client = client
        self._model = model
        self._triage_fallback = triage_fallback
        self._usage_sink = usage_sink
        self.last_triage_source: Literal["openai", "fallback"] | None = None
        self.last_triage_note: str | None = None

    def triage(self, incident_id: str, fingerprint: Fingerprint) -> TriageResult:
        if self._client is None:
            if self._triage_fallback is None:
                raise RuntimeError("no OpenAI client and no triage fallback")
            self.last_triage_source = "fallback"
            self.last_triage_note = "fallback (no OPENAI_API_KEY)"
            return self._triage_fallback.model_copy(update={"incident_id": incident_id})
        try:
            result = run_triage(
                self._client,
                fingerprint,
                self._candidates,
                incident_id,
                model=self._model,
                usage_sink=self._usage_sink,
            )
        except Exception:
            if self._triage_fallback is None:
                raise
            self.last_triage_source = "fallback"
            self.last_triage_note = "fallback (OpenAI error)"
            return self._triage_fallback.model_copy(update={"incident_id": incident_id})
        self.last_triage_source = "openai"
        self.last_triage_note = "openai"
        return result

    def plan(
        self,
        triage: TriageResult,
        catalog: list[LeverSpec],
        blast_radius: Callable[[str, dict], float],
    ) -> Experiment | None:
        catalog_ids = {spec.id for spec in catalog}
        candidates = [
            candidate.model_copy(
                update={
                    "blast_radius_pct": blast_radius(candidate.lever_id, candidate.params),
                }
            )
            for candidate in self._candidates
            if candidate.lever_id in catalog_ids
        ]
        return plan_experiment(triage, candidates).selected

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
        healthy = [fp for fp in baseline if not any(slo.breached for slo in fp.slos)]
        incident = [fp for fp in baseline if any(slo.breached for slo in fp.slos)]
        if not healthy:
            healthy = list(baseline)
        if not incident:
            incident = list(baseline[-3:])
        return judge(
            triage,
            ordered_series,
            [experiment_window],
            NoiseModel.from_windows(healthy),
            NoiseModel.from_windows(incident),
        )


def build_live_brain(
    candidates: list[Experiment],
    *,
    api_key: str | None,
    model: str,
    triage_fallback: TriageResult | None,
) -> LiveBrain:
    client = None
    if api_key:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("install with: uv sync --extra llm") from exc
        client = OpenAI(api_key=api_key)
    return LiveBrain(
        candidates,
        client=client,
        model=model,
        triage_fallback=triage_fallback,
    )
