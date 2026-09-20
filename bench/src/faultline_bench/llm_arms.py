"""OpenAI-backed passive and LLM-only benchmark arms.

Both arms see exactly the C1 fingerprints Faultline sees and nothing else. Neither
passes the math judge: the diagnosis is the model's opinion, which is the point of
the ablation. Labels are benchmark outcomes and never enter runtime telemetry.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from faultline_brain.telemetry import assert_no_leak
from faultline_brain.triage import _content, _usage
from faultline_contracts import NONE_OF_THE_ABOVE, Experiment, Fingerprint
from faultline_contracts.openai_schema import strict_response_format

DEFAULT_MODEL = "gpt-4.1"

HYPOTHESES: dict[str, str] = {
    "H_meta": (
        "Self-sustaining retry storm. A transient database slowdown pushed queries past the "
        "client timeout; Orders' retries now keep the database overloaded, so the slowness "
        "sustains itself after the trigger ended. Capping retries would heal it permanently."
    ),
    "H_db": (
        "Degraded database. Something outside the application cut database capacity below "
        "demand; retries amplify the load but are a symptom, not the cause. Capping retries "
        "removes the amplification but the database stays slow; failing over heals it."
    ),
    NONE_OF_THE_ABOVE: (
        "Neither. The sustaining cause is somewhere else (for example a CPU-starved service); "
        "neither a retry cap nor a database failover would heal it."
    ),
}

PASSIVE_SYSTEM_PROMPT = """You are an on-call engineer diagnosing a live incident from dashboards alone.
You receive one steady-state telemetry fingerprint of the incident and the healthy
5 s windows that preceded it. You may not run any experiment. Choose exactly one of
the listed diagnoses and cite the metric keys you relied on. Use only the supplied
metric keys. If the fingerprint is consistent with more than one diagnosis you must
still commit to the single most likely one."""

CHOOSE_SYSTEM_PROMPT = """You are an autonomous incident responder. You receive the steady-state incident
fingerprint, the healthy 5 s windows that preceded it, the candidate diagnoses, and a
list of reversible experiments; each applies one lever for hold_s seconds and then
releases it. Pick the single experiment whose response will best tell the diagnoses
apart, weighing what you would learn against blast_radius_pct (the share of user
requests affected). Return the experiment id exactly as given. Set experiment_id to
null only when no experiment would help."""

JUDGE_SYSTEM_PROMPT = """You are an autonomous incident responder interpreting an experiment you just ran on
a live incident. You receive the incident fingerprint, the healthy 5 s windows that
preceded it, the candidate diagnoses, the experiment that was applied, the 5 s windows
recorded while it was held, and the windows recorded after it was released. Work out
what each diagnosis predicts for the during and after-release phases, compare with
what was measured, and commit to exactly one diagnosis, citing the metric keys you
relied on. If the experiment was not run, decide from the fingerprint alone."""

Label = Literal["H_meta", "H_db", "none_of_the_above"]


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnosis: Label
    evidence: list[str]
    reasoning: str


class ExperimentChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str | None
    rationale: str


def _metrics(fingerprint: Fingerprint) -> dict[str, float]:
    return {k: v for k, v in fingerprint.metrics().items() if v is not None}


def _windows(fingerprints: list[Fingerprint]) -> list[dict[str, Any]]:
    return [
        {"window_start": fp.window_start.isoformat(), "metrics": _metrics(fp)}
        for fp in fingerprints
    ]


def _experiment(experiment: Experiment) -> dict[str, Any]:
    return {
        "id": experiment.id,
        "lever_id": experiment.lever_id,
        "params": experiment.params,
        "hold_s": experiment.hold_s,
        "blast_radius_pct": experiment.blast_radius_pct,
    }


class OpenAIArms:
    """Passive-only and LLM-only policies backed by strict OpenAI structured output.

    Every call reports token usage to ``usage_sink`` so the benchmark can record
    tokens per incident. ``last`` keeps the parsed model outputs for the report.
    """

    def __init__(
        self,
        client: Any,
        *,
        model: str = DEFAULT_MODEL,
        usage_sink: Callable[[dict[str, int | None]], None] | None = None,
    ):
        self._client = client
        self._model = model
        self._usage_sink = usage_sink
        self.last: dict[str, Any] = {}

    def _ask(self, system_prompt: str, payload: dict[str, Any], schema: type[BaseModel], name: str) -> BaseModel:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format=strict_response_format(schema, name),
        )
        if self._usage_sink is not None:
            self._usage_sink(_usage(response))
        parsed = schema.model_validate_json(_content(response))
        self.last[name] = parsed.model_dump()
        return parsed

    def passive_diagnose(self, incident: Fingerprint, healthy: list[Fingerprint]) -> str:
        assert_no_leak(incident)
        payload = {
            "diagnoses": HYPOTHESES,
            "incident_fingerprint": incident.model_dump(mode="json"),
            "healthy_windows": _windows(healthy),
            "known_metrics": sorted(_metrics(incident)),
        }
        return self._ask(PASSIVE_SYSTEM_PROMPT, payload, Diagnosis, "passive_diagnosis").diagnosis

    def choose(
        self, incident: Fingerprint, healthy: list[Fingerprint], candidates: list[Experiment]
    ) -> Experiment | None:
        assert_no_leak(incident)
        by_id = {c.id: c for c in candidates}
        payload = {
            "diagnoses": HYPOTHESES,
            "incident_fingerprint": incident.model_dump(mode="json"),
            "healthy_windows": _windows(healthy),
            "candidate_experiments": [_experiment(c) for c in candidates],
        }
        choice = self._ask(CHOOSE_SYSTEM_PROMPT, payload, ExperimentChoice, "experiment_choice")
        if choice.experiment_id is None:
            return None
        if choice.experiment_id not in by_id:
            payload["previous_error"] = (
                f"experiment_id {choice.experiment_id!r} is not one of {sorted(by_id)}; return one of them or null"
            )
            choice = self._ask(CHOOSE_SYSTEM_PROMPT, payload, ExperimentChoice, "experiment_choice")
        return by_id.get(choice.experiment_id) if choice.experiment_id is not None else None

    def judge(
        self,
        incident: Fingerprint,
        healthy: list[Fingerprint],
        experiment: Experiment | None,
        during: list[Fingerprint],
        after: list[Fingerprint],
    ) -> str:
        assert_no_leak(incident)
        payload = {
            "diagnoses": HYPOTHESES,
            "incident_fingerprint": incident.model_dump(mode="json"),
            "healthy_windows": _windows(healthy),
            "experiment": _experiment(experiment) if experiment is not None else None,
            "during_windows": _windows(during),
            "after_release_windows": _windows(after),
        }
        return self._ask(JUDGE_SYSTEM_PROMPT, payload, Diagnosis, "experiment_judgement").diagnosis
