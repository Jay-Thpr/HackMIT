"""C2 triage: strict OpenAI structured output plus semantic validation.

This module deliberately owns only the proposal side of diagnosis.  It asks the
model for hypotheses and testable predictions; `judge.py` remains the sole
authority that can turn measurements into a verdict.
"""

from __future__ import annotations

import json
from datetime import datetime
from collections.abc import Callable
from typing import Any

from faultline_contracts.fingerprint import Fingerprint
from faultline_contracts.levers import Experiment
from faultline_contracts.openai_schema import triage_response_format
from faultline_contracts.triage import TriageDraft, TriageResult

from .telemetry import assert_no_leak

DEFAULT_MODEL = "gpt-4.1"
SYSTEM_PROMPT = """You are Faultline's incident-triage proposer. Analyze only the supplied
telemetry fingerprint. Propose plausible sustaining causes and directional,
measurable predictions for the supplied reversible experiments. Do not claim a
diagnosis is proven: measurement will judge it. Use only supplied canonical metric
keys and experiment ids. When telemetry cannot separate hypotheses, set ambiguous
to true and make each hypothesis provide a positive, falsifiable confirms_if test
of that same hypothesis. Never use a rival hypothesis being ruled out, or its
signature merely returning after release, as confirmation. Measurement uses a
failed positive test to produce none-of-the-above."""


class TriageValidationError(ValueError):
    """The model response was malformed or failed C2 semantic validation."""


def _usage(response: Any) -> dict[str, int | None]:
    """Extract OpenAI token usage without coupling C2 to a particular SDK model."""
    usage = getattr(response, "usage", None)
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _content(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise TriageValidationError("OpenAI response did not contain a message") from exc
    if not isinstance(content, str) or not content.strip():
        raise TriageValidationError("OpenAI response message was empty")
    return content


def _validate(
    content: str, known_metrics: set[str], experiment_ids: set[str]
) -> TriageDraft:
    try:
        draft = TriageDraft.model_validate_json(content)
    except ValueError as exc:
        raise TriageValidationError(f"response did not match TriageDraft: {exc}") from exc
    problems = draft.problems(known_metrics=known_metrics, experiment_ids=experiment_ids)
    if problems:
        raise TriageValidationError("; ".join(problems))
    return draft


def run_triage(
    client: Any,
    fingerprint: Fingerprint,
    candidates: list[Experiment],
    incident_id: str,
    *,
    model: str = DEFAULT_MODEL,
    system_prompt: str = SYSTEM_PROMPT,
    created_at: datetime | None = None,
    max_attempts: int = 2,
    usage_sink: Callable[[dict[str, int | None]], None] | None = None,
) -> TriageResult:
    """Request and validate C2 triage from OpenAI.

    The caller supplies an initialized OpenAI-compatible client to keep the
    package testable and avoid making the OpenAI SDK a transitive contract
    dependency. On semantic validation failure, one corrective re-prompt (or the
    configured number of attempts) is made with the precise problems.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    assert_no_leak(fingerprint)
    known_metrics = set(fingerprint.metrics())
    experiment_ids = {experiment.id for experiment in candidates}
    user_content = {
        "fingerprint": fingerprint.model_dump(mode="json"),
        "candidate_experiments": [
            {
                "id": experiment.id,
                "lever_id": experiment.lever_id,
                "params": experiment.params,
                "hold_s": experiment.hold_s,
                "blast_radius_pct": experiment.blast_radius_pct,
            }
            for experiment in candidates
        ],
        "known_metrics": sorted(known_metrics),
    }
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_content)},
    ]
    last_error: TriageValidationError | None = None

    for attempt in range(max_attempts):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format=triage_response_format(),
        )
        if usage_sink is not None:
            usage_sink(_usage(response))
        try:
            draft = _validate(_content(response), known_metrics, experiment_ids)
        except TriageValidationError as exc:
            last_error = exc
            if attempt + 1 == max_attempts:
                break
            messages.append(
                {
                    "role": "user",
                    "content": f"Your previous response failed validation: {exc}. Return a corrected complete JSON object.",
                }
            )
            continue
        values = draft.model_dump()
        if created_at is not None:
            values["created_at"] = created_at
        return TriageResult(incident_id=incident_id, **values)

    raise last_error or TriageValidationError("triage failed without a validation error")
