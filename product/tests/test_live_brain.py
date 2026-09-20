import json
from pathlib import Path

import pytest
from faultline_contracts import (
    NONE_OF_THE_ABOVE,
    AuditEvent,
    Experiment,
    Fingerprint,
    TriageResult,
    experiment_windows,
)
from faultline_product.adapters import (
    FixtureLeverAdapter,
    LiveBrain,
    build_live_brain,
)
from faultline_product.adapters.brain import INCIDENT_STEADY_WINDOWS, _baselines
from faultline_product.cli import main
from faultline_product.fixtures import load_fixture

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures"


def _storm_inputs():
    series = [
        Fingerprint.model_validate(item)
        for item in json.loads((FIXTURES / "series_storm_experiment.json").read_text())
    ]
    triage = TriageResult.model_validate(json.loads((FIXTURES / "triage_hero.json").read_text()))
    events = [
        AuditEvent.model_validate(json.loads(line))
        for line in (FIXTURES / "audit_hero.jsonl").read_text().splitlines()
        if line
    ]
    windows = experiment_windows(events)
    experiment_start = min(window.start for window in windows)
    pre_experiment = [fp for fp in series if fp.window_end <= experiment_start]
    baseline = pre_experiment[:12]
    incident = pre_experiment[12:]
    during = [fp for fp in series if experiment_start <= fp.window_start < windows[0].release]
    after_release = [fp for fp in series if fp.window_start >= windows[0].release]
    return triage, series, baseline, incident, during, after_release, windows


def test_plan_filters_catalog_and_recomputes_blast_radius():
    bundle = load_fixture("storm")
    adapter = FixtureLeverAdapter()
    candidates = [
        bundle.experiment.model_copy(update={"blast_radius_pct": 99}),
        Experiment(
            id="unknown",
            lever_id="unknown",
            params={},
            hold_s=20,
            blast_radius_pct=0,
        ),
    ]
    brain = LiveBrain(candidates)

    selected = brain.plan(
        bundle.triage,
        adapter.catalog(),
        adapter.estimate_blast_radius,
    )

    assert selected is not None
    assert selected.id == "retry_cap_0_20s"
    assert selected.blast_radius_pct == adapter.estimate_blast_radius(
        selected.lever_id, selected.params
    )


def test_plan_returns_none_without_predictions():
    bundle = load_fixture("storm")
    brain = LiveBrain(bundle.experiments)
    triage = bundle.triage.model_copy(update={"predictions": []})

    assert brain.plan(triage, FixtureLeverAdapter().catalog(), lambda *_: 0) is None


def test_judge_matches_storm_fixture():
    triage, series, baseline, incident, during, after_release, windows = _storm_inputs()
    verdict = LiveBrain([]).judge(
        triage,
        Experiment.model_validate(json.loads((FIXTURES / "experiments.json").read_text())[0]),
        [*baseline, *incident],
        during,
        after_release,
    )

    assert verdict.diagnosis == "H_meta"
    assert verdict.confirmed is True
    assert verdict.summary == "H_meta confirmed across 10 observation(s)."
    assert len(series) == 34
    assert len(incident) == 12
    assert len(windows) == 1


def test_judge_empty_during_returns_none_of_the_above():
    bundle = load_fixture("storm")
    verdict = LiveBrain([]).judge(
        bundle.triage,
        bundle.experiment,
        [bundle.telemetry.first_breach()],
        [],
        [bundle.telemetry.first_breach()],
    )

    assert verdict.diagnosis == NONE_OF_THE_ABOVE
    assert verdict.confirmed is False
    assert verdict.summary == "insufficient telemetry: no during/after-release windows"


def test_judge_refuses_to_confirm_without_true_healthy_baseline():
    triage, series, _baseline, incident, during, after_release, _windows = _storm_inputs()

    verdict = LiveBrain([]).judge(
        triage,
        Experiment.model_validate(json.loads((FIXTURES / "experiments.json").read_text())[0]),
        incident,
        during,
        after_release,
    )

    assert verdict.diagnosis == NONE_OF_THE_ABOVE
    assert verdict.confirmed is False
    assert verdict.summary == "insufficient telemetry: no healthy baseline windows"


def test_incident_baseline_uses_only_the_stable_breached_tail():
    _triage, series, healthy, incident, _during, _after_release, _windows = _storm_inputs()

    selected_healthy, selected_incident = _baselines([*healthy, *incident])

    assert selected_healthy == healthy
    assert selected_incident == incident[-INCIDENT_STEADY_WINDOWS:]


class _Response:
    def __init__(self, content):
        self.choices = [
            type("Choice", (), {"message": type("Message", (), {"content": content})()})()
        ]


class _Client:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.chat = type("Chat", (), {})()
        self.chat.completions = type("Completions", (), {"create": self.create})()

    def create(self, **kwargs):
        del kwargs
        if self.error:
            raise self.error
        return self.response


def test_triage_openai_and_fallback_paths():
    bundle = load_fixture("storm")
    draft = json.loads((FIXTURES / "triage_hero.json").read_text())
    draft.pop("incident_id")
    draft.pop("created_at")
    draft.pop("schema_version")
    client = _Client(_Response(json.dumps(draft)))
    brain = LiveBrain(bundle.experiments, client=client)

    result = brain.triage("openai-incident", bundle.telemetry.first_breach())

    assert result.incident_id == "openai-incident"
    assert brain.last_triage_source == "openai"

    fallback = LiveBrain(
        bundle.experiments,
        client=_Client(error=RuntimeError("unavailable")),
        triage_fallback=bundle.triage,
    )
    result = fallback.triage("fallback-incident", bundle.telemetry.first_breach())
    assert result.incident_id == "fallback-incident"
    assert fallback.last_triage_source == "fallback"

    with pytest.raises(RuntimeError, match="no OpenAI client"):
        LiveBrain(bundle.experiments).triage("no-client", bundle.telemetry.first_breach())


def test_build_live_brain_without_key_uses_fallback():
    bundle = load_fixture("storm")
    brain = build_live_brain(
        bundle.experiments,
        api_key=None,
        model="gpt-test",
        triage_fallback=bundle.triage,
    )
    assert brain.triage("fallback", bundle.telemetry.first_breach()).incident_id == "fallback"


def test_cli_live_brain_falls_back_without_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Do not let a developer's repository .env turn this no-key test into a live-key test.
    monkeypatch.chdir(tmp_path)
    result = main(
        [
            "--audit-log",
            str(tmp_path / "audit.jsonl"),
            "watch",
            "--brain",
            "live",
            "--incident",
            "brain-fallback",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "[triage] source: fallback (no OPENAI_API_KEY)" in output
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    verdict = next(event for event in events if event["kind"] == "verdict")
    assert verdict["payload"]["diagnosis"] == "H_meta"
    assert verdict["payload"]["confirmed"] is True


def test_plan_scores_ranks_every_catalog_candidate():
    bundle = load_fixture("storm")
    adapter = FixtureLeverAdapter()
    brain = LiveBrain(bundle.experiments)

    rows = brain.plan_scores(bundle.triage, adapter.catalog(), adapter.estimate_blast_radius)

    assert [row["experiment_id"] for row in rows][0] == "retry_cap_0_20s"
    assert {row["experiment_id"] for row in rows} == {item.id for item in bundle.experiments}
    assert [row["score"] for row in rows] == sorted((row["score"] for row in rows), reverse=True)
    for row in rows:
        assert set(row) == {"experiment_id", "lever_id", "separation", "score", "blast_radius_pct"}
        assert row["blast_radius_pct"] == adapter.estimate_blast_radius(
            row["lever_id"], next(i.params for i in bundle.experiments if i.id == row["experiment_id"])
        )
        assert row["score"] == row["separation"] - 0.1 * row["blast_radius_pct"]
