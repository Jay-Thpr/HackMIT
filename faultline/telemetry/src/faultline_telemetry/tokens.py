"""Measured prompt-size comparisons for one incident.

This stays at the telemetry boundary: it compares observed raw snapshots with
the C1 fingerprints derived from them.  It does not inspect hidden fault state
or make an LLM decision.  Counts use OpenAI's ``o200k_base`` tokenizer so the
benchmark reports tokens rather than an imprecise character estimate.
"""

from dataclasses import dataclass
import json
from collections.abc import Mapping, Sequence
from typing import Any

import tiktoken

from faultline_contracts.fingerprint import Fingerprint


DEFAULT_ENCODING = "o200k_base"


@dataclass(frozen=True)
class IncidentTokenComparison:
    """Exact tokenizer counts for the raw and compressed incident evidence."""

    raw_tokens: int
    fingerprint_tokens: int
    windows: int

    @property
    def tokens_saved(self) -> int:
        return self.raw_tokens - self.fingerprint_tokens

    @property
    def reduction_ratio(self) -> float | None:
        """Fraction of raw-prompt tokens avoided, or None for an empty raw input."""
        return self.tokens_saved / self.raw_tokens if self.raw_tokens else None


def _canonical_json(value: Any) -> str:
    """Serialize without whitespace so repeated benchmark runs are comparable."""
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def token_count(value: Any, *, encoding_name: str = DEFAULT_ENCODING) -> int:
    """Count tokens for a JSON prompt payload using a named OpenAI encoding."""
    return len(tiktoken.get_encoding(encoding_name).encode(_canonical_json(value)))


def fingerprint_prompt_payload(fingerprints: Sequence[Fingerprint]) -> list[dict[str, Any]]:
    """Return the compact C1 evidence payload that is sent to triage.

    Timestamps are retained because the Brain needs window order; omitted C1
    fields remain omitted, never materialized as zeroes.
    """
    return [fingerprint.model_dump(mode="json", exclude_none=True) for fingerprint in fingerprints]


def compare_incident_tokens(
    raw_snapshots: Sequence[Mapping[str, Any]],
    fingerprints: Sequence[Fingerprint],
    *,
    encoding_name: str = DEFAULT_ENCODING,
) -> IncidentTokenComparison:
    """Compare raw public telemetry snapshots with the C1 prompt for one incident.

    ``raw_snapshots`` must be the public observations captured for the same
    incident.  This makes the reported saving auditable and avoids inventing a
    fake raw-telemetry baseline.
    """
    return IncidentTokenComparison(
        raw_tokens=token_count(list(raw_snapshots), encoding_name=encoding_name),
        fingerprint_tokens=token_count(fingerprint_prompt_payload(fingerprints), encoding_name=encoding_name),
        windows=len(fingerprints),
    )
