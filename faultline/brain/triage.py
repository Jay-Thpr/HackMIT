"""OpenAI structured-output client for C2 triage drafts."""

from __future__ import annotations

import json
from typing import Any

from faultline_contracts.fingerprint import Fingerprint
from faultline_contracts.openai_schema import triage_response_format
from faultline_contracts.triage import TriageDraft, TriageResult


class TriageError(RuntimeError):
    """The model response could not be safely used as a C2 triage result."""


SYSTEM_PROMPT = """You are Faultline's incident-triage proposer. Use only the supplied telemetry fingerprint and candidate experiments. Do not claim hidden causes as facts. Produce hypotheses, their directional predictions, and positive confirmation tests. A metric must use an exact canonical key supplied in known_metrics. The downstream statistical judge, not you, decides the diagnosis."""


class OpenAITriageClient:
    """Thin, injectable wrapper around the OpenAI Chat Completions client.

    The client is injected so unit tests do not need credentials or the OpenAI
    SDK.  The contract's strict JSON-schema helper is passed directly to the
    API and the result is validated again locally before use.
    """

    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model

    def triage(self, incident_id: str, fingerprint: Fingerprint, experiments: list[dict[str, Any]]) -> TriageResult:
        payload = {
            "fingerprint": fingerprint.model_dump(mode="json"),
            "known_metrics": sorted(fingerprint.metrics()),
            "experiments": experiments,
        }
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, separators=(",", ":"))},
                ],
                response_format=triage_response_format(),
            )
            content = response.choices[0].message.content
            if not content:
                raise TriageError("OpenAI returned no structured triage content")
            draft = TriageDraft.model_validate_json(content)
        except TriageError:
            raise
        except Exception as exc:
            raise TriageError(f"OpenAI triage request failed: {exc}") from exc
        problems = draft.problems(known_metrics=set(fingerprint.metrics()), experiment_ids={item["id"] for item in experiments})
        if problems:
            raise TriageError("invalid triage draft: " + "; ".join(problems))
        return TriageResult(**draft.model_dump(), incident_id=incident_id)
