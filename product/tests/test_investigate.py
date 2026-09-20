import json
from datetime import datetime, timezone
from pathlib import Path

from faultline_contracts import (
    ActionHandle,
    EventKind,
    Experiment,
    Fingerprint,
    JsonlSink,
    Stage,
    UndoSpec,
)
from faultline_contracts.clone import CloneEndpoints, CloneInfo, CloneStatus, LabActionHandle
from faultline_product.adapters import (
    FixtureCanaryDeployer,
    FixtureDevinAdapter,
    FixtureLeverAdapter,
    LabInvestigation,
)
from faultline_product.adapters.fixture import FixtureBrain, FixtureClock
from faultline_product.fixtures import load_fixture
from faultline_product.orchestrator import Orchestrator
from faultline_product.paths import CONTRACT_FIXTURES
from faultline_product.ports import HypothesisInvestigation
from faultline_product.renderer import TerminalRenderer

T0 = datetime(2026, 9, 19, 20, 0, tzinfo=timezone.utc)


def _series() -> list[Fingerprint]:
    raw = json.loads((CONTRACT_FIXTURES / "series_storm_experiment.json").read_text())
    return [Fingerprint.model_validate(item) for item in raw]


class FakeLab:
    def __init__(self):
        self.events: list[str] = []
        self.n = 0

    def create(self, spec):
        self.n += 1
        self.events.append(f"create:{spec.name}")
        return CloneInfo(
            clone_id=f"{spec.name}-{self.n}", status=CloneStatus.ready, spec=spec, created_at=T0,
            endpoints=CloneEndpoints(
                gateway_url="http://c:9080", control_url="http://c:10901",
                stats_urls={"orders": "http://c:9101", "payments": "http://c:9102", "loadgen": "http://c:9103"},
            ),
        )

    def apply(self, clone_id, action, params, ttl_s):
        self.events.append(f"apply:{clone_id}:{action}")
        return LabActionHandle(action_id=f"a{len(self.events)}", clone_id=clone_id, action=action, params=params, ttl_s=ttl_s)

    def undo(self, handle):
        self.events.append(f"undo:{handle.clone_id}:{handle.action}")
        return handle

    def reset(self, clone_id):
        self.events.append(f"reset:{clone_id}")

    def destroy(self, clone_id):
        self.events.append(f"destroy:{clone_id}")


class ScriptedCloneTelemetry:
    """latest() and series() return successive scripted values (per clone)."""

    def __init__(self, latest, series):
        self._latest, self._series = iter(latest), iter(series)
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def latest(self):
        return next(self._latest)

    def series(self, start, end):
        return next(self._series)


class RecordingLevers:
    def __init__(self):
        self.calls = []

    def apply(self, lever_id, params, ttl_s):
        self.calls.append(("apply", lever_id, params))
        return ActionHandle(action_id="x", lever_id=lever_id, params=params, applied_at=T0, ttl_s=ttl_s, undo=UndoSpec(lever_id=lever_id))

    def undo(self, handle):
        self.calls.append(("undo", handle.lever_id, handle.params))
        return handle


def _investigation(lab, telemetry_by_hypothesis, levers):
    clock = FixtureClock(T0)
    return LabInvestigation(
        lab,
        telemetry_factory=lambda clone, incident_id: telemetry_by_hypothesis[clone.spec.name.split("-")[0]],
        levers_factory=lambda clone: levers,
        sleep=clock.sleep,
        clock=clock,
        max_clones=2,
    )


def test_investigation_runs_one_clone_per_hypothesis_and_measures_probe_in_clone():
    bundle = load_fixture("storm")
    series = _series()
    healthy, incident = series[0], series[12]
    probe = Experiment.model_validate(next(e for e in json.loads((CONTRACT_FIXTURES / "experiments.json").read_text()) if e["id"] == "retry_cap_0_20s"))
    # H_meta clone: reproduces (incident), recovers (healthy); probe windows come from the storm fixture
    h_meta = ScriptedCloneTelemetry(
        latest=[incident, healthy],
        series=[series[:12], series[12:24], series[24:28], series[28:]],
    )
    # H_db clone: never reproduces (stays healthy)
    h_db = ScriptedCloneTelemetry(
        latest=[healthy, healthy],
        series=[series[:12], series[:12], series[:4], series[:4]],
    )
    lab, levers = FakeLab(), RecordingLevers()

    results = _investigation(lab, {"h_meta": h_meta, "h_db": h_db}, levers).investigate(
        "inc-1", bundle.triage, incident, [healthy], probe
    )

    by_id = {r.hypothesis_id: r for r in results}
    assert by_id["H_meta"].reproduced and by_id["H_meta"].recovered
    assert by_id["H_meta"].prediction_total == 5 and by_id["H_meta"].prediction_matches == 5
    assert by_id["H_meta"].survives
    assert not by_id["H_db"].reproduced and not by_id["H_db"].survives
    assert by_id["H_meta"].recipe == {"action": "db_latency", "params": {"extra_ms": 800}, "ttl_s": 20}
    assert by_id["H_db"].recipe["action"] == "db_capacity"
    # the production probe ran inside the clone through the clone's C3 control, apply then undo
    assert ("apply", "retry_cap", {"max_retries": 0}) in levers.calls
    assert ("undo", "retry_cap", {"max_retries": 0}) in levers.calls
    # every clone was torn down; the re-injected cause for the probe was undone
    creates = [e for e in lab.events if e.startswith("create")]
    assert len(creates) == 2
    # the clone is reset to a verified healthy state before the probe's baseline is taken
    h_meta_clone = by_id["H_meta"].clone_id
    events = lab.events
    assert events.index(f"reset:{h_meta_clone}") < len(events) - 1 - events[::-1].index(f"apply:{h_meta_clone}:db_latency")
    for clone_id in (by_id["H_meta"].clone_id, by_id["H_db"].clone_id):
        assert f"destroy:{clone_id}" in lab.events and f"reset:{clone_id}" in lab.events
    assert h_meta.stopped and h_db.stopped
    assert "5/5 predicted directions" in by_id["H_meta"].detail


def test_investigation_failure_is_reported_not_raised():
    bundle = load_fixture("storm")
    series = _series()

    class BrokenTelemetry(ScriptedCloneTelemetry):
        def latest(self):
            return None  # clone never produced a window

    lab = FakeLab()
    results = _investigation(
        lab, {"h_meta": BrokenTelemetry([], []), "h_db": BrokenTelemetry([], [])}, RecordingLevers()
    ).investigate("inc-2", bundle.triage, series[12], [series[0]], bundle.experiment)
    assert all(not r.reproduced and "aborted" in r.detail for r in results)
    assert sum(e.startswith("destroy") for e in lab.events) == 2


class StubInvestigation:
    def __init__(self, results):
        self.results, self.calls = results, []

    def investigate(self, incident_id, triage, production_incident, healthy_reference, production_probe):
        self.calls.append((incident_id, [h.id for h in triage.hypotheses], production_probe.id, len(healthy_reference)))
        return self.results


def _run(tmp_path, investigation, gate):
    bundle = load_fixture("storm")
    clock = FixtureClock(bundle.experiment_start, bundle.telemetry.last_window_end)
    audit = JsonlSink(tmp_path / "audit.jsonl")
    orchestrator = Orchestrator(
        FixtureLeverAdapter(clock=clock), audit, FixtureDevinAdapter(), FixtureCanaryDeployer(),
        TerminalRenderer(lambda _l: None), bundle.telemetry,
        FixtureBrain(bundle.triage, bundle.experiment, bundle.verdict), clock, clock.sleep,
        investigation=investigation, investigation_gate=gate,
    )
    return orchestrator.run("inv", bundle.experiment_start), audit.query("inv")


def _hi(hid, reproduced, clone="c-1"):
    return HypothesisInvestigation(hid, clone, {"action": "x"}, reproduced, True, 5, 5, "d", {"k": 1})


def test_orchestrator_records_investigations_before_production_probe(tmp_path):
    inv = StubInvestigation([_hi("H_meta", True), _hi("H_db", True)])
    result, events = _run(tmp_path, inv, gate=False)

    assert inv.calls[0][1:3] == (["H_meta", "H_db"], "retry_cap_0_20s")
    assert inv.calls[0][3] > 0  # healthy reference windows handed to the investigators
    recorded = [e for e in events if e.payload.get("investigation")]
    assert [e.payload["hypothesis_id"] for e in recorded] == ["H_meta", "H_db"]
    assert all(e.stage == Stage.experiment and e.actor.value == "math" for e in recorded)
    first_apply = next(i for i, e in enumerate(events) if e.kind == EventKind.action_apply)
    assert all(events.index(e) < first_apply for e in recorded)  # clones before production is touched
    assert result.diagnosis == "H_meta" and len(result.investigations) == 2


def test_gate_drops_unreproduced_hypothesis_and_still_confirms_in_production(tmp_path):
    inv = StubInvestigation([_hi("H_meta", True), _hi("H_db", False)])
    result, events = _run(tmp_path, inv, gate=True)

    dropped = [e for e in events if e.kind == EventKind.refused and "dropped before production" in e.summary]
    assert dropped and dropped[0].payload == {"dropped": ["H_db"], "survivors": ["H_meta"]}
    assert any(e.kind == EventKind.action_apply for e in events)  # production probe still ran
    assert result.diagnosis == "H_meta"


def test_gate_pages_human_when_nothing_reproduces(tmp_path):
    inv = StubInvestigation([_hi("H_meta", False), _hi("H_db", False)])
    result, events = _run(tmp_path, inv, gate=True)

    assert result.diagnosis == "none_of_the_above" and result.patch is None
    assert not any(e.kind == EventKind.action_apply for e in events)  # production never touched
    assert any(e.kind == EventKind.page_human and "no hypothesis reproduced" in e.summary for e in events)


def test_investigator_crash_becomes_evidence_not_exception():
    bundle = load_fixture("storm")
    series = _series()

    class ExplodingTelemetry(ScriptedCloneTelemetry):
        def latest(self):
            raise TimeoutError("timed out")  # bare OSError from a stalled clone, as seen live

    lab = FakeLab()
    results = _investigation(
        lab, {"h_meta": ExplodingTelemetry([], []), "h_db": ExplodingTelemetry([], [])}, RecordingLevers()
    ).investigate("inc-3", bundle.triage, series[12], [series[0]], bundle.experiment)
    assert all("crashed: TimeoutError" in r.detail and not r.survives for r in results)
    assert sum(e.startswith("destroy") for e in lab.events) == 2


def test_orchestrator_survives_a_broken_investigation_stage(tmp_path):
    class Broken:
        def investigate(self, *args):
            raise ConnectionError("lab manager gone")

    result, events = _run(tmp_path, Broken(), gate=True)
    assert result.diagnosis == "H_meta"  # v5 loop still ran to a verdict
    assert any(e.kind == EventKind.refused and "clone investigation unavailable" in e.summary for e in events)
    assert any(e.kind == EventKind.action_apply for e in events)


def test_agent_investigation_records_attempts_and_persists_recipe():
    from faultline_brain import SeedInvestigator

    bundle = load_fixture("storm")
    series = _series()
    healthy, incident = series[0], series[12]
    probe = Experiment.model_validate(next(e for e in json.loads((CONTRACT_FIXTURES / "experiments.json").read_text()) if e["id"] == "retry_cap_0_20s"))
    # H_meta clone: reproduces (incident), recovers (healthy); probe windows from the storm fixture
    h_meta = ScriptedCloneTelemetry(
        latest=[incident, healthy],
        series=[series[:12], series[12:24], series[24:28], series[28:]],
    )
    # H_db clone: never reproduces (stays healthy); the seed agent then stops
    h_db = ScriptedCloneTelemetry(latest=[healthy], series=[])
    lab, levers, sink = FakeLab(), RecordingLevers(), []
    clock = FixtureClock(T0)
    inv = LabInvestigation(
        lab,
        telemetry_factory=lambda clone, incident_id: {"h_meta": h_meta, "h_db": h_db}[clone.spec.name.split("-")[0]],
        levers_factory=lambda clone: levers,
        sleep=clock.sleep,
        clock=clock,
        max_clones=2,
        agent=SeedInvestigator(),
        recipe_sink=sink.append,
    )

    results = inv.investigate("inc-5", bundle.triage, incident, [healthy], probe)

    by_id = {r.hypothesis_id: r for r in results}
    assert by_id["H_meta"].reproduced and by_id["H_meta"].recovered and by_id["H_meta"].survives
    assert by_id["H_meta"].recipe == {"action": "db_latency", "params": {"extra_ms": 800}, "ttl_s": 20}
    assert by_id["H_meta"].attempts and by_id["H_meta"].attempts[0]["action"] == "db_latency"
    assert by_id["H_meta"].attempts[0]["reproduced"] is True
    assert "attempts 1/3" in by_id["H_meta"].detail
    assert sink and sink[0]["hypothesis_id"] == "H_meta" and sink[0]["recipe"] == by_id["H_meta"].recipe
    assert not by_id["H_db"].reproduced and by_id["H_db"].recipe is None
    assert len(by_id["H_db"].attempts) == 1  # one miss, then the seed agent stops
    for clone_id in (by_id["H_meta"].clone_id, by_id["H_db"].clone_id):
        assert f"destroy:{clone_id}" in lab.events


def test_reset_failure_during_cleanup_keeps_the_evidence():
    bundle = load_fixture("storm")
    series = _series()
    healthy, incident = series[0], series[12]

    class ResetFails(FakeLab):
        def reset(self, clone_id):
            from faultline_contracts.clone import LabError

            self.events.append(f"reset:{clone_id}")
            raise LabError(f"POST /clones/{clone_id}/reset -> 503: could not reach a healthy baseline")

        def get(self, clone_id):
            return None

    lab = ResetFails()
    h_meta = ScriptedCloneTelemetry(latest=[incident, healthy], series=[series[:12], series[12:24], series[24:28], series[28:]])
    h_db = ScriptedCloneTelemetry(latest=[healthy, healthy], series=[series[:12], series[:12], series[:4], series[:4]])
    results = _investigation(lab, {"h_meta": h_meta, "h_db": h_db}, RecordingLevers()).investigate(
        "inc-4", bundle.triage, incident, [healthy], bundle.experiment
    )
    by_id = {r.hypothesis_id: r for r in results}
    assert by_id["H_meta"].reproduced and by_id["H_meta"].recovered  # evidence survived the failed reset
    assert by_id["H_meta"].prediction_total is None and "not measured" in by_id["H_meta"].detail
    assert by_id["H_meta"].survives  # reproduction + recovery stand; the probe is simply unmeasured
    assert sum(e.startswith("destroy") for e in lab.events) == 2


def _agent_attempt(action="db_latency", reproduced=False):
    from faultline_brain import (
        AttemptRecord,
        LabParams,
        LabProposal,
        MetricDirection,
        SimilarityEvidence,
    )

    proposal = LabProposal(
        action=action,
        params=LabParams(
            extra_ms=800, capacity_qps=None, service=None, cpus=None,
            max_retries=None, timeout_ms=None,
        ),
        ttl_s=20,
        observe_after_s=10,
        predicted=[MetricDirection(metric="db.qps", direction="up")],
        rationale="seeded guess",
        stop=False,
        stop_reason="",
    )
    return AttemptRecord(proposal, SimilarityEvidence(10, 4, 1.5, reproduced), 1, 0, reproduced)


def _agent_result(attempts, evidence=None, clone_id="c-1"):
    from faultline_brain import AgentInvestigationEvidence

    return AgentInvestigationEvidence(list(attempts), evidence, None, None, clone_id)


def _stub_agentic(monkeypatch, script):
    """Replace AgenticCloneInvestigator: records each construction and replays `script`."""
    import faultline_product.adapters.investigate as investigate_module

    instances = []

    class StubAgentic:
        def __init__(self, lab, observe, agent, *, budget, wait):
            self.agent = agent
            self.budget = budget
            self.investigate_calls = 0
            instances.append(self)

        def investigate(self, hypothesis, spec, production_incident, healthy, **kwargs):
            self.investigate_calls += 1
            return script.pop(0)

    monkeypatch.setattr(investigate_module, "AgenticCloneInvestigator", StubAgentic)
    return instances


def _agent_investigation(agent):
    clock = FixtureClock(T0)
    return LabInvestigation(
        FakeLab(),
        telemetry_factory=lambda clone, incident_id: ScriptedCloneTelemetry([], []),
        levers_factory=lambda clone: RecordingLevers(),
        sleep=clock.sleep,
        clock=clock,
        max_clones=1,  # sequential: the stubbed investigator's script stays deterministic
        agent=agent,
        recipe_sink=lambda record: None,
    )


def test_agent_miss_falls_back_to_seeded_investigator(monkeypatch):
    from faultline_brain import SeedInvestigator

    bundle = load_fixture("storm")
    series = _series()
    healthy, incident = series[0], series[12]
    script = [
        _agent_result([_agent_attempt(), _agent_attempt()]),  # H_meta agent misses twice
        _agent_result([_agent_attempt()]),                    # H_meta seeded fallback
        _agent_result([_agent_attempt()]),                    # H_db agent misses
        _agent_result([_agent_attempt()]),                    # H_db seeded fallback
    ]
    instances = _stub_agentic(monkeypatch, script)

    class NonSeedAgent:
        pass

    results = _agent_investigation(NonSeedAgent()).investigate(
        "inc-6", bundle.triage, incident, [healthy], bundle.experiment
    )

    # each hypothesis: agent result + one seeded retry in a fresh investigator
    assert [len(r.attempts) for r in results] == [3, 2]
    assert all(not r.reproduced for r in results)
    assert "did not reproduce" in results[0].detail
    assert len(instances) == 4
    assert isinstance(instances[0].agent, NonSeedAgent)
    assert isinstance(instances[1].agent, SeedInvestigator)
    assert instances[1].budget == 1
    assert isinstance(instances[3].agent, SeedInvestigator)
    assert all(i.investigate_calls == 1 for i in instances)


def test_seed_investigator_gets_no_fallback(monkeypatch):
    from faultline_brain import SeedInvestigator

    bundle = load_fixture("storm")
    series = _series()
    healthy, incident = series[0], series[12]
    script = [_agent_result([_agent_attempt()]), _agent_result([_agent_attempt()])]
    instances = _stub_agentic(monkeypatch, script)

    results = _agent_investigation(SeedInvestigator()).investigate(
        "inc-7", bundle.triage, incident, [healthy], bundle.experiment
    )

    # a seed agent is already the baseline: one investigator per hypothesis, no retry
    assert len(instances) == 2
    assert all(i.investigate_calls == 1 for i in instances)
    assert [len(r.attempts) for r in results] == [1, 1]
    assert all(not r.reproduced for r in results)
