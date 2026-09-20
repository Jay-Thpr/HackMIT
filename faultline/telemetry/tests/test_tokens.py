from datetime import UTC, datetime, timedelta

from faultline_contracts import Fingerprint, ServiceStats

from faultline_telemetry.tokens import compare_incident_tokens, fingerprint_prompt_payload, token_count


def _fingerprint() -> Fingerprint:
    start = datetime(2026, 9, 19, tzinfo=UTC)
    return Fingerprint(
        window_start=start,
        window_end=start + timedelta(seconds=5),
        services={"orders": ServiceStats(qps=80.0, retry_ratio=1.2)},
    )


def test_token_count_is_deterministic_for_mapping_order():
    assert token_count({"a": 1, "b": 2}) == token_count({"b": 2, "a": 1})


def test_comparison_uses_actual_raw_snapshots_and_compact_c1_payload():
    fingerprint = _fingerprint()
    raw = [{"service": "orders", "counters": {"requests": 400}, "histogram_samples": [31] * 100}]

    comparison = compare_incident_tokens(raw, [fingerprint])

    assert comparison.raw_tokens == token_count(raw)
    assert comparison.fingerprint_tokens == token_count(fingerprint_prompt_payload([fingerprint]))
    assert comparison.windows == 1
    assert comparison.raw_tokens > comparison.fingerprint_tokens
    assert comparison.tokens_saved > 0
    assert comparison.reduction_ratio is not None and 0 < comparison.reduction_ratio < 1


def test_empty_raw_input_has_no_reduction_ratio():
    comparison = compare_incident_tokens([], [_fingerprint()])
    assert comparison.raw_tokens == 1  # JSON array literal, counted by the OpenAI tokenizer.
    assert comparison.reduction_ratio is not None
