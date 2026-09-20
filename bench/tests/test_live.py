"""Live-driver tests: no network, no real sleeping — stubs and a fake clock."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import faultline_bench.live as live_module
from faultline_contracts import NONE_OF_THE_ABOVE, AuditEvent, EventKind
from faultline_contracts.audit import Actor, Stage
from faultline_contracts.fingerprint import Fingerprint, SloStatus

from faultline_bench.live import (
    EXPECTED,
    GRID,
    RPS,
    LiveCase,
    LiveWorld,
    BenchRuntime,
    NO_INCIDENT,
    _aggregate,
    build_plan,
    diagnosis_from_audit,
    load_fixtures,
    run_active_arm,
    run_llm_only_arm,
    run_passive_arms,
    run_random_arm,
    score,
)

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures"
T0 = datetime(2026, 9, 20, tzinfo=timezone.utc)


def load_fingerprint(name: str) -> Fingerprint:
    return Fingerprint.model_validate(json.loads((FIXTURES / name).read_text()))


def load_series(name: str) -> list[Fingerprint]:
    return [Fingerprint.model_validate(item) for item in json.loads((FIXTURES / name).read_text())]


def clone_at(fp: Fingerprint, start: datetime, *, breached: bool | None = None) -> Fingerprint:
    update = {"window_start": start, "window_end": start + timedelta(seconds=5)}
    if breached is not None:
        update["slos"] = [
            slo.model_copy(update={"breached": breached}) for slo in fp.slos
        ] or [SloStatus(name="checkout", metric="svc.gateway.p99_ms", threshold=1000.0, value=1.0, breached=breached)]
    return fp.model_copy(update=update)


class FakeClock:
    def __init__(self, start: datetime = T0):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class StubTelemetry:
    """Replays prebuilt fingerprints; LiveWorld filters them by window_start."""

    def __init__(self, fps: list[Fingerprint]):
        self._fps = fps

    def series(self, start, end, step_s=5):
        return [fp for fp in self._fps if start <= fp.window_start < end]


class GenTelemetry:
    """Generates 5 s windows on demand: healthy before `breach_at`, breached after."""

    def __init__(self, healthy: Fingerprint, incident: Fingerprint, breach_at: datetime):
        self._healthy = healthy
        self._incident = incident
        self._breach_at = breach_at

    def series(self, start, end, step_s=5):
        out = []
        t = start
        while t < end:
            base = self._incident if t >= self._breach_at else self._healthy
            out.append(clone_at(base, t))
            t += timedelta(seconds=5)
        return out


class StubDriver:
    def __init__(self, *, reset_exc: Exception | None = None):
        self.reset_exc = reset_exc
        self.resets = 0
        self.injected = []
        self.loads = []

    def reset(self):
        self.resets += 1
        if self.reset_exc is not None:
            raise self.reset_exc
        return SimpleNamespace()

    def inject(self, case):
        self.injected.append(case)
        return SimpleNamespace()

    def set_load(self, rps):
        self.loads.append(rps)


class RecordingLevers:
    def __init__(self):
        self.calls = []

    def apply(self, lever_id, params, ttl_s):
        self.calls.append(("apply", lever_id, params, ttl_s))
        return SimpleNamespace(lever_id=lever_id)

    def undo(self, handle):
        self.calls.append(("undo", handle.lever_id))


def http_error(status: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        f"{status}", request=httpx.Request("POST", "http://x"), response=httpx.Response(status)
    )


def make_runtime(driver, telemetry_fps=None, telemetry=None, clock=None):
    clock = clock or FakeClock()
    levers = RecordingLevers()
    telemetry = telemetry or StubTelemetry(telemetry_fps or [])
    world_holder = {}

    def make_world():
        world = LiveWorld(telemetry, levers, sleep=clock.sleep, clock=clock)
        world_holder["world"] = world
        return world, lambda: None

    triage, candidates = load_fixtures()
    rt = BenchRuntime(
        driver=driver,
        triage=triage,
        candidates=candidates,
        make_arms=lambda sink: (_ for _ in ()).throw(AssertionError("LLM arms must not be built")),
        make_world=make_world,
        sleep=clock.sleep,
    )
    return rt, world_holder, levers


# -- 1. build_plan -------------------------------------------------------------

def test_build_plan_interleaves_and_covers_the_grid():
    cases = build_plan(["storm", "degraded", "cpu", "no_fault"], 10)
    assert len(cases) == 40
    assert [c.world for c in cases[:4]] == ["storm", "degraded", "cpu", "no_fault"]
    by_world = {w: [c for c in cases if c.world == w] for w in EXPECTED}
    assert all(len(v) == 10 for v in by_world.values())
    storm = by_world["storm"]
    # Derived from the grid rather than hardcoded: the storm region is re-tuned
    # whenever an ignition sweep rules a corner in or out.
    assert all(c.params["delay_ms"] in set(GRID["storm"]["delay_ms"]) for c in storm)
    assert all(c.params["duration_s"] in set(GRID["storm"]["duration_s"]) for c in storm)
    assert all(c.rps in set(RPS) for c in storm)
    assert all(c.develop_s == c.params["duration_s"] + 10 for c in storm)
    storm_combos = len(GRID["storm"]["delay_ms"]) * len(GRID["storm"]["duration_s"]) * len(RPS)
    assert len({tuple(sorted(c.params.items())) + (("rps", c.rps),) for c in storm}) == min(
        10, storm_combos
    )
    for world in ("degraded", "cpu", "no_fault"):
        assert all(c.develop_s == 5 for c in by_world[world])
    assert all(c.expected == EXPECTED[c.world] for c in cases)


def test_build_plan_cap_keeps_worlds_balanced():
    cases = build_plan(["storm", "degraded", "cpu", "no_fault"], 10, cap=2)
    assert [c.world for c in cases] == ["storm", "degraded"]


def test_build_plan_draws_the_strongest_combos_first():
    cases = build_plan(["storm", "degraded"], 10, cap=2)
    storm, degraded = cases
    assert storm.params == {"delay_ms": 1000, "duration_s": 30} and storm.rps == 100
    assert degraded.params == {"capacity_qps": 30} and degraded.rps == 100


# -- 2. audit + score ----------------------------------------------------------

def audit_event(kind, **payload):
    return AuditEvent(
        incident_id="i", stage=Stage.detect, kind=kind, actor=Actor.orchestrator, summary="", payload=payload
    )


def test_diagnosis_from_audit():
    verdict = diagnosis_from_audit([
        audit_event(EventKind.detect),
        audit_event(EventKind.verdict, diagnosis="H_meta", confirmed=True),
    ])
    assert verdict == "H_meta"
    assert diagnosis_from_audit([]) == NO_INCIDENT
    assert diagnosis_from_audit([audit_event(EventKind.detect)]) is None


def test_score_maps_each_world():
    for i, (world, expected) in enumerate(EXPECTED.items()):
        case = LiveCase(i, world, {}, 60, expected, 5)
        assert score(case, expected) is True
        assert score(case, "H_meta" if expected != "H_meta" else "H_db") is False
    no_fault = LiveCase(9, "no_fault", {}, 60, NO_INCIDENT, 5)
    assert score(no_fault, NO_INCIDENT) is True
    assert score(no_fault, NONE_OF_THE_ABOVE) is False


# -- 3. unscored on infrastructure failure -------------------------------------

def test_infra_failure_marks_arms_unscored_without_calling_the_llm():
    case = LiveCase(0, "storm", {"delay_ms": 800, "duration_s": 20}, 60, "H_meta", 30)
    driver = StubDriver(reset_exc=http_error(503))
    rt, _, _ = make_runtime(driver)

    for records in (run_passive_arms(case, rt, ["passive"]), run_random_arm(case, rt)):
        assert len(records) == 1
        record = records[0]
        assert record["unscored"] is True
        assert "503" in record["unscored_reason"]
        assert record["correct"] is None  # unscored runs carry no verdict either way


# -- 4. LiveWorld exclusion windows + incident gate ----------------------------

def remapped_series(name: str, start: datetime) -> list[Fingerprint]:
    return [clone_at(fp, start + timedelta(seconds=5 * i)) for i, fp in enumerate(load_series(name))]


def test_develop_excludes_exactly_its_windows():
    clock = FakeClock()
    fps = remapped_series("series_storm_experiment.json", clock.t)
    world = LiveWorld(StubTelemetry(fps), RecordingLevers(), sleep=clock.sleep, clock=clock)
    t0 = clock.t
    world.develop(30)
    assert len(world.series(t0, clock.t)) == 0  # all 6 windows in [t0, t0+30) excluded
    assert len(world.series(clock.t, clock.t + timedelta(seconds=30))) == 6


def test_develop_snaps_drifted_wall_time_to_the_window_grid():
    clock = FakeClock()
    fps = remapped_series("series_storm_experiment.json", clock.t)
    start = clock.t
    world = LiveWorld(StubTelemetry(fps), RecordingLevers(), sleep=clock.sleep, clock=clock)
    clock.sleep(60.3)  # drifted 0.3 s past the start+60 grid boundary
    world.develop(30)
    visible = {fp.window_start for fp in remapped_series("series_storm_experiment.json", start)
               if fp.window_start < clock.t}
    excluded = visible - {fp.window_start for fp in world.series(start, clock.t)}
    assert excluded == {start + timedelta(seconds=s) for s in (60, 65, 70, 75, 80, 85)}
    assert start + timedelta(seconds=90) not in excluded  # first post-develop window kept


def test_incident_declared_gate():
    clock = FakeClock()
    fps = remapped_series("series_storm_experiment.json", clock.t)
    world = LiveWorld(StubTelemetry(fps), RecordingLevers(), sleep=clock.sleep, clock=clock)
    clock.sleep(120)  # windows 0..23 visible; the last 12 are the breached incident windows
    assert world.incident_declared() is True

    clock2 = FakeClock()
    healthy = load_fingerprint("fingerprint_healthy.json")
    fps2 = [clone_at(healthy, clock2.t + timedelta(seconds=5 * i)) for i in range(24)]
    world2 = LiveWorld(StubTelemetry(fps2), RecordingLevers(), sleep=clock2.sleep, clock=clock2)
    clock2.sleep(120)
    assert world2.incident_declared() is False


# -- 5. random arm end-to-end through run_hero_case -----------------------------

def test_random_arm_end_to_end_records_a_scored_verdict():
    clock = FakeClock()
    healthy = load_fingerprint("fingerprint_healthy.json")
    incident_fp = load_series("series_storm_experiment.json")[12]
    case = LiveCase(0, "storm", {"delay_ms": 600, "duration_s": 15}, 60, "H_meta", 25)
    breach_at = clock.t + timedelta(seconds=60 + case.develop_s)
    driver = StubDriver()
    rt, _, levers = make_runtime(driver, telemetry=GenTelemetry(healthy, incident_fp, breach_at), clock=clock)

    records = run_random_arm(case, rt)

    assert len(records) == 1
    record = records[0]
    assert record["arm"] == "random"
    assert record["unscored"] is False
    assert isinstance(record["diagnosis"], str)
    assert record["verdict"]["selected_experiment_id"] in {c.id for c in rt.candidates}
    assert driver.resets == 2  # pre and post reset
    assert driver.injected == [case]
    assert any(c[0] == "apply" for c in levers.calls)


class StubArms:
    def __init__(self, candidates):
        self._candidates = candidates
        self.last = {}
        self.calls = []

    def choose(self, incident, healthy, cands):
        self.calls.append("choose")
        return self._candidates[0]

    def judge(self, incident, healthy, chosen, during, after):
        self.calls.append("judge")
        return "H_meta"


def test_llm_only_arm_end_to_end():
    clock = FakeClock()
    healthy = load_fingerprint("fingerprint_healthy.json")
    incident_fp = load_series("series_storm_experiment.json")[12]
    case = LiveCase(0, "storm", {"delay_ms": 600, "duration_s": 15}, 60, "H_meta", 25)
    breach_at = clock.t + timedelta(seconds=60 + case.develop_s)
    driver = StubDriver()
    rt, _, levers = make_runtime(driver, telemetry=GenTelemetry(healthy, incident_fp, breach_at), clock=clock)
    stub_arms = StubArms(rt.candidates)
    rt.make_arms = lambda sink: stub_arms

    records = run_llm_only_arm(case, rt)

    assert len(records) == 1
    record = records[0]
    assert record["arm"] == "llm_only"
    assert record["unscored"] is False
    assert record["diagnosis"] == "H_meta"
    assert record["verdict"]["selected_experiment_id"] == rt.candidates[0].id
    assert any(c[0] == "apply" for c in levers.calls)
    assert driver.resets == 2
    assert stub_arms.calls == ["choose", "judge"]


# -- 5b. active arm: LiveLoop check failures -> unscored --------------------------

def _stub_live_loop(monkeypatch, checks):
    """Patch _import_live_loop so run_active_arm sees canned checks, no subprocess."""
    captured = {}

    class StubLoop:
        def __init__(self, *args, **kwargs):
            self.report = SimpleNamespace(checks=checks)

        def run_world(self, name, start_watch, *, spec=None, incident=None):
            captured["spec"] = spec
            captured["incident"] = incident
            return Path("/nonexistent/audit-bench-stub.jsonl")

    monkeypatch.setattr(
        live_module, "_import_live_loop", lambda: SimpleNamespace(LiveLoop=StubLoop)
    )
    return captured


def _ok(name):
    return SimpleNamespace(name=name, ok=True, detail="")


def _fail(name, detail=""):
    return SimpleNamespace(name=name, ok=False, detail=detail)


def _active_rt(monkeypatch, checks, no_elastic=False):
    captured = _stub_live_loop(monkeypatch, checks)
    rt, _, _ = make_runtime(StubDriver())
    rt.no_elastic = no_elastic
    return rt, captured


STORM_CASE = LiveCase(0, "storm", {"delay_ms": 1000, "duration_s": 30}, 100, "H_meta", 40)


def test_active_arm_scored_when_loop_checks_pass(monkeypatch):
    rt, _ = _active_rt(
        monkeypatch,
        [
            _ok("watch still waiting for a breach (no false detect on healthy traffic)"),
            _ok("incident visible on /stats"),
        ],
    )
    (record,) = run_active_arm(STORM_CASE, rt)
    assert record["unscored"] is False
    assert record["diagnosis"] == NO_INCIDENT  # stub wrote no audit file
    assert record["correct"] is False
    assert record["checks_failed"] == []


def test_active_arm_unscored_when_fault_never_ignites(monkeypatch):
    rt, _ = _active_rt(
        monkeypatch,
        [
            _ok("watch still waiting for a breach (no false detect on healthy traffic)"),
            _fail("incident visible on /stats"),
        ],
    )
    (record,) = run_active_arm(STORM_CASE, rt)
    assert record["unscored"] is True
    assert "no ignition" in record["unscored_reason"]
    assert record["diagnosis"] == NO_INCIDENT
    assert record["correct"] is None
    assert record["checks_failed"] == ["incident visible on /stats"]


def test_active_arm_unscored_when_cli_dies_before_injection(monkeypatch):
    rt, _ = _active_rt(
        monkeypatch,
        [_fail("watch still waiting for a breach (no false detect on healthy traffic)", "exit=1")],
    )
    (record,) = run_active_arm(STORM_CASE, rt)
    assert record["unscored"] is True
    assert "exit=1" in record["unscored_reason"]
    assert record["correct"] is None


def test_active_arm_spec_carries_no_elastic_env_only_when_set(monkeypatch):
    rt, captured = _active_rt(monkeypatch, [_ok("x")], no_elastic=True)
    run_active_arm(STORM_CASE, rt)
    assert captured["spec"]["env"] == {
        "FAULTLINE_ELASTICSEARCH_URL": "",
        "FAULTLINE_ELASTICSEARCH_API_KEY": "",
    }

    rt, captured = _active_rt(monkeypatch, [_ok("x")], no_elastic=False)
    run_active_arm(STORM_CASE, rt)
    assert "env" not in captured["spec"]


# -- 6. report aggregation -------------------------------------------------------

def test_aggregate_counts_scored_and_unscored():
    runs = [
        {"arm": "active", "world": "storm", "unscored": False, "correct": True},
        {"arm": "active", "world": "degraded", "unscored": False, "correct": False},
        {"arm": "active", "world": "cpu", "unscored": True, "correct": None},
    ]
    accuracy, by_world = _aggregate(runs)
    assert accuracy["active"] == {"correct": 1, "n": 2, "accuracy": 0.5, "unscored": 1}
    assert by_world["active"]["storm"] == {"correct": 1, "n": 1, "unscored": 0}
    assert by_world["active"]["cpu"] == {"correct": 0, "n": 0, "unscored": 1}
