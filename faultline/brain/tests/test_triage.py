"""Tests for strict OpenAI C2 triage wiring; no network access required."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from faultline_contracts.fingerprint import Fingerprint
from faultline_contracts.levers import Experiment

from faultline_brain.triage import TriageValidationError, run_triage

FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "fixtures"


class FakeClient:
    def __init__(self, contents: list[str]):
        self.contents = iter(contents)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(self.contents)))])


def hero_inputs():
    fingerprint = Fingerprint.model_validate_json((FIXTURES / "fingerprint_storm.json").read_text())
    candidates = [Experiment.model_validate(item) for item in json.loads((FIXTURES / "experiments.json").read_text())]
    draft = json.loads((FIXTURES / "triage_hero.json").read_text())
    for key in ("schema_version", "incident_id", "created_at"):
        draft.pop(key)
    return fingerprint, candidates, json.dumps(draft)


def test_run_triage_uses_strict_schema_and_returns_c2_result():
    fingerprint, candidates, content = hero_inputs()
    client = FakeClient([content])

    result = run_triage(client, fingerprint, candidates, "inc-test")

    assert result.incident_id == "inc-test"
    assert result.ambiguous is True
    assert len(client.calls) == 1
    assert client.calls[0]["response_format"]["json_schema"]["strict"] is True
    request = json.loads(client.calls[0]["messages"][1]["content"])
    assert request["candidate_experiments"][0]["id"] == "retry_cap_0_20s"
    assert "db.query_p50_ms" in request["known_metrics"]


def test_run_triage_reports_token_usage_for_every_attempt():
    fingerprint, candidates, content = hero_inputs()
    client = FakeClient([content])
    client.create = lambda **kwargs: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=34, total_tokens=46),
    )
    client.chat.completions.create = client.create
    usage = []

    run_triage(client, fingerprint, candidates, "inc-test", usage_sink=usage.append)

    assert usage == [{"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46}]


def test_run_triage_retries_once_with_semantic_errors():
    fingerprint, candidates, content = hero_inputs()
    invalid = json.loads(content)
    invalid["predictions"][0]["during"][0]["metric"] = "db.not_real"
    client = FakeClient([json.dumps(invalid), content])

    result = run_triage(client, fingerprint, candidates, "inc-test")

    assert result.incident_id == "inc-test"
    assert len(client.calls) == 2
    assert "unknown metric" in client.calls[1]["messages"][-1]["content"]


def test_run_triage_raises_after_retry_budget_is_exhausted():
    fingerprint, candidates, content = hero_inputs()
    invalid = json.loads(content)
    invalid["hypotheses"][0]["id"] = "none_of_the_above"
    client = FakeClient([json.dumps(invalid)])

    with pytest.raises(TriageValidationError, match="reserved"):
        run_triage(client, fingerprint, candidates, "inc-test", max_attempts=1)
