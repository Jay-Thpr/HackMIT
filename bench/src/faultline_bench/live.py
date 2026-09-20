"""Live-sandbox benchmark driver: run the same arms against the real sandbox.

Each case performs a hidden C5 injection and scores each requested arm's
diagnosis against the expected label. Bench is the only component allowed to
drive C5; the arms themselves see only C1 telemetry and C3 levers.

  uv run faultline-bench-live --dry-run --cases 4
  uv run faultline-bench-live --worlds storm degraded --arms active random
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random
from typing import Any

import httpx

from faultline_contracts import (
    NONE_OF_THE_ABOVE,
    AuditEvent,
    EventKind,
    Experiment,
    Fingerprint,
    TriageResult,
    utcnow,
)
from faultline_contracts.fault import (
    CpuStarveFault,
    DegradeDbFault,
    HttpFaultController,
    StormFault,
    World,
)
from faultline_product.adapters.live_telemetry import (
    LiveTelemetrySource,
    TelemetryUnavailable,
)
from faultline_product.adapters.sandbox import SandboxLeverAdapter

from .baselines import NearestCentroid
from .runner import BenchmarkResult, run_hero_case, run_llm_only_case, run_passive_only_case

ROOT = Path(__file__).resolve().parents[3]
INTEGRATION = ROOT / "integration"
FIXTURES = ROOT / "contracts" / "fixtures"
DEFAULT_RUNS_DIR = INTEGRATION / "runs"
OUT_DIR = ROOT / "runs"

NO_INCIDENT = "no_incident"

EXPECTED = {
    "storm": "H_meta",
    "degraded": "H_db",
    "cpu": NONE_OF_THE_ABOVE,
    "no_fault": NO_INCIDENT,
}

# Strongest combos first so capped/short runs draw the proven-to-ignite end.
#
# The storm grid is restricted to the region at or above the one combo the
# sandbox vouches for: 800 ms / 20 s @ 80 rps, re-measured 2026-09-20 as a clean
# 0/5 baseline, 5/5 breached during the trigger and 13/13 still breached over the
# 65 s after it stopped (peak retry_ratio 4.11 -- the storm sustaining itself).
# 600 ms / 15 s @ 60 rps is known not to ignite (error_rate 0.09, retry_ratio 1.0),
# and a storm case that never ignites costs ~6.5 min of the active arm to produce
# an unscored row. Every combo kept here is at least as strong as the verified one
# in all three dimensions; the dropped corner can be re-measured with a C5-only
# ignition sweep and added back.
GRID = {
    "storm": dict(delay_ms=(1000, 800), duration_s=(30, 20)),
    "degraded": dict(capacity_qps=(30, 40, 50)),
    "cpu": dict(cpus=(0.1, 0.2)),
    "no_fault": dict(),
}

RPS = (100, 80)

ARMS = ("active", "passive", "centroid", "llm_only", "random")

_WORLD_ENUM = {
    "storm": World.storm,
    "degraded": World.degraded_db,
    "cpu": World.cpu_starve,
    "no_fault": World.none,
}

# Deterministic per-world seed offsets for the random arm (no hash()).
_WORLD_OFFSET = {"storm": 0, "degraded": 1, "cpu": 2, "no_fault": 3}

# Rough per-case wall-clock estimates used by --dry-run, in minutes.
_ESTIMATE_MIN = {"active": 6.5, "passive": 3.5, "centroid": 0.0, "llm_only": 4.0, "random": 4.0}


@dataclass(frozen=True)
class LiveCase:
    index: int
    world: str
    params: dict
    rps: int
    expected: str
    develop_s: int


def build_plan(worlds: list[str], per_world: int, cap: int | None = None) -> list[LiveCase]:
    """Deterministic case grid, interleaved across worlds so a cap stays balanced."""
    per: dict[str, list[tuple[dict, int, int]]] = {}
    for world in worlds:
        keys = list(GRID[world])
        combos = list(itertools.product(*(GRID[world][key] for key in keys), RPS))
        n = per_world
        if n <= len(combos):
            picked = [combos[(i * len(combos)) // n] for i in range(n)]
        else:
            picked = [combos[i % len(combos)] for i in range(n)]
        per[world] = []
        for combo in picked:
            params = dict(zip(keys, combo[: len(keys)]))
            rps = combo[len(keys)]
            develop_s = params["duration_s"] + 10 if world == "storm" else 5
            per[world].append((params, rps, develop_s))
    cases: list[LiveCase] = []
    index = 0
    for i in range(per_world):
        for world in worlds:
            params, rps, develop_s = per[world][i]
            cases.append(LiveCase(index, world, params, rps, EXPECTED[world], develop_s))
            index += 1
    return cases[:cap] if cap is not None else cases


class LiveWorld:
    """The real sandbox behind the FakeWorld surface the runner expects."""

    def __init__(
        self,
        telemetry: LiveTelemetrySource,
        levers: SandboxLeverAdapter,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.telemetry = telemetry
        self.levers = levers
        self._sleep = sleep
        self._clock = clock
        self.start = clock()
        self._excluded: list[tuple[datetime, datetime]] = []

    @property
    def now(self) -> datetime:
        return self._clock()

    def advance(self, seconds: float) -> None:
        self._sleep(seconds)

    def _snap(self, t: datetime) -> datetime:
        """Snap down to the 5 s window grid anchored at ``self.start``."""
        delta = (t - self.start).total_seconds()
        return self.start + timedelta(seconds=(delta // 5) * 5)

    def develop(self, seconds: float) -> None:
        """Sleep while the fault develops; the ramp is excluded from every baseline.

        Bounds snap down to the window grid: with develop_s a multiple of 5 this
        excludes exactly develop_s/5 windows starting at the injection window,
        even when wall time has drifted past a grid boundary.
        """
        t0 = self._snap(self.now)
        self._sleep(seconds)
        self._excluded.append((t0, self._snap(self.now)))

    def series(self, start: datetime, end: datetime) -> list[Fingerprint]:
        fps = self.telemetry.series(start, end)
        return [
            fp
            for fp in fps
            if not any(t0 <= fp.window_start < t1 for t0, t1 in self._excluded)
        ]

    def latest(self) -> Fingerprint:
        fps = self.series(self.start, self.now)
        if not fps:
            raise TelemetryUnavailable("no telemetry windows resolved yet")
        return fps[-1]

    def apply(self, lever_id: str, params: dict, ttl_s: int):
        return self.levers.apply(lever_id, params, ttl_s)

    def undo(self, handle) -> None:
        self.levers.undo(handle)

    def incident_declared(self) -> bool:
        windows = self.series(self.start, self.now)[-12:]
        return sum(1 for fp in windows if any(slo.breached for slo in fp.slos)) >= 10


def make_live_world(
    control_url: str,
    stats_urls: dict[str, str] | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = utcnow,
) -> tuple[LiveWorld, Callable[[], None]]:
    """Start 1 s telemetry polling and return the world plus a stop callback."""
    telemetry = LiveTelemetrySource(**(stats_urls or {}))
    telemetry.start(period_s=1.0)
    # Take one synchronous snapshot before LiveWorld stamps `start`: series()
    # needs a snapshot at or before the window start, so without this the first
    # 5 s window never resolves and the runner's healthy/incident slices shift.
    telemetry.snapshot()
    levers = SandboxLeverAdapter(base_url=control_url, clock=clock)
    return LiveWorld(telemetry, levers, sleep=sleep, clock=clock), telemetry.stop


class FaultDriver:
    """Thin C5 wrapper; injectable in tests via subclassing or duck typing."""

    def __init__(self, fault_url: str, timeout_s: float = 150):
        self.fault_url = fault_url.rstrip("/")
        self.fc = HttpFaultController(self.fault_url, timeout_s=timeout_s)

    def reset(self):
        return self.fc.reset()

    def state(self):
        return self.fc.state()

    def inject(self, case: LiveCase):
        params = case.params
        if case.world == "storm":
            return self.fc.storm(StormFault(delay_ms=params["delay_ms"], duration_s=params["duration_s"]))
        if case.world == "degraded":
            return self.fc.degrade_db(DegradeDbFault(capacity_qps=params["capacity_qps"]))
        if case.world == "cpu":
            return self.fc.cpu_starve(CpuStarveFault(service="payments", cpus=params["cpus"]))
        return self.fc.state()

    def set_load(self, rps: int) -> None:
        response = httpx.post(f"{self.fault_url}/debug/load", json={"rps": rps}, timeout=10)
        response.raise_for_status()


def _import_live_loop():
    """integration/ is not a package; import live_loop by path, lazily."""
    if str(INTEGRATION) not in sys.path:
        sys.path.insert(0, str(INTEGRATION))
    import live_loop

    return live_loop


def load_fixtures() -> tuple[TriageResult, list[Experiment]]:
    triage = TriageResult.model_validate_json((FIXTURES / "triage_hero.json").read_text())
    candidates = [Experiment.model_validate(item) for item in json.loads((FIXTURES / "experiments.json").read_text())]
    return triage, candidates


def events_for_incident(audit_path: str | Path | None, incident_id: str) -> list[AuditEvent]:
    if audit_path is None or not Path(audit_path).exists():
        return []
    events = [
        AuditEvent.model_validate_json(line)
        for line in Path(audit_path).read_text().splitlines()
        if line.strip()
    ]
    return [e for e in events if e.incident_id == incident_id]


def diagnosis_from_audit(events: list[AuditEvent]) -> str | None:
    if not any(e.kind == EventKind.detect for e in events):
        return NO_INCIDENT
    verdict = next((e for e in events if e.kind == EventKind.verdict), None)
    return verdict.payload.get("diagnosis") if verdict else None


def verdict_from_audit(events: list[AuditEvent]) -> dict:
    verdict = next((e for e in events if e.kind == EventKind.verdict), None)
    return {
        "diagnosis": verdict.payload.get("diagnosis") if verdict else None,
        "confirmed": verdict.payload.get("confirmed") if verdict else None,
        "experiments": sorted(
            {e.experiment_id for e in events if e.kind == EventKind.experiment_start and e.experiment_id}
        ),
        "actions": sum(1 for e in events if e.kind == EventKind.action_apply),
    }


def score(case: LiveCase, diagnosis: str | None) -> bool:
    return diagnosis == case.expected


def centroid_training_cases(runs_dir: Path = DEFAULT_RUNS_DIR) -> list[tuple[str, dict]]:
    """Flat metric dicts from prior live reports, labelled by known world."""
    cases: list[tuple[str, dict]] = []
    for path in sorted(runs_dir.glob("live-*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for window in data.get("windows", []):
            world, _, label = str(window.get("phase", "")).partition("/")
            if label != "incident develops" or EXPECTED.get(world) not in ("H_meta", "H_db"):
                continue
            if (window.get("svc.orders.error_rate") or 0) < 0.5:
                continue
            cases.append((EXPECTED[world], window))
    return cases


def fit_centroid_from_runs(runs_dir: Path = DEFAULT_RUNS_DIR) -> NearestCentroid | None:
    cases = centroid_training_cases(runs_dir)
    if len({label for label, _ in cases}) < 2:
        return None
    return NearestCentroid.fit_metrics(cases)


def _load_api_key() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    try:
        from faultline_telemetry.dotenv import load_repo_dotenv
    except ImportError:
        load_repo_dotenv = None
    if load_repo_dotenv is not None:
        load_repo_dotenv(ROOT)
    else:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def make_openai_client():
    _load_api_key()
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    from openai import OpenAI

    return OpenAI()


@dataclass
class BenchRuntime:
    """Everything one arm-run needs; tests inject stubs for every edge."""

    driver: FaultDriver
    triage: TriageResult
    candidates: list[Experiment]
    make_arms: Callable[[Callable[[dict], None]], Any] | None = None
    centroid: NearestCentroid | None = None
    make_world: Callable[[], tuple[LiveWorld, Callable[[], None]]] | None = None
    verbose: bool = False
    baseline_s: int = 130
    cli_timeout_s: int = 420
    no_elastic: bool = False
    sleep: Callable[[float], None] = time.sleep


def _incident_id(case: LiveCase) -> str:
    return f"bench-{case.world}-{case.index}-{datetime.now(timezone.utc):%H%M%S}"


def _record(
    case: LiveCase,
    arm: str,
    incident_id: str,
    *,
    diagnosis: str | None = None,
    verdict: dict | None = None,
    tokens: int | None = None,
    wall_s: float | None = None,
    started_at: datetime | None = None,
    audit_path: str | None = None,
    llm_outputs: dict | None = None,
    unscored: bool = False,
    unscored_reason: str | None = None,
    error: str | None = None,
    extra: dict | None = None,
) -> dict:
    record = {
        "case": case.index,
        "world": case.world,
        "params": case.params,
        "rps": case.rps,
        "arm": arm,
        "incident_id": incident_id,
        "expected": case.expected,
        "diagnosis": diagnosis,
        "correct": None if unscored else score(case, diagnosis),
        "unscored": unscored,
        "unscored_reason": unscored_reason,
        "verdict": verdict,
        "tokens": tokens,
        "wall_s": wall_s,
        "started_at": started_at.isoformat() if started_at else None,
        "audit_path": audit_path,
        "llm_outputs": llm_outputs,
        "error": error,
    }
    if extra:
        record.update(extra)
    return record


def _is_infra(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.HTTPError, TelemetryUnavailable)) or type(exc).__name__ == "StepAborted"


def _unscored_record(case: LiveCase, arm: str, incident_id: str, exc: BaseException, started_at, wall_s) -> dict:
    if _is_infra(exc):
        return _record(
            case, arm, incident_id, unscored=True, unscored_reason=f"infrastructure: {exc!r}",
            started_at=started_at, wall_s=wall_s,
        )
    return _record(
        case, arm, incident_id, unscored=True, unscored_reason=f"exception: {exc}",
        error=str(exc), started_at=started_at, wall_s=wall_s,
    )


def _result_verdict(result: BenchmarkResult) -> dict:
    return {
        "selected_experiment_id": result.selected_experiment_id,
        "confirmed": result.confirmed,
        "blast_radius_pct": result.blast_radius_pct,
        "action_seconds": result.action_seconds,
    }


def _tokens(usage: list[dict]) -> int | None:
    return sum(u.get("total_tokens") or 0 for u in usage) if usage else None


def run_active_arm(case: LiveCase, rt: BenchRuntime) -> list[dict]:
    """One injection through the real `faultline watch` CLI via LiveLoop."""
    incident = _incident_id(case)
    started_at = utcnow()
    t0 = time.monotonic()
    try:
        live_loop = _import_live_loop()
        loop = live_loop.LiveLoop(rt.verbose, rt.baseline_s, rt.cli_timeout_s)
        spec = dict(
            inject=lambda fc: rt.driver.inject(case),
            world=_WORLD_ENUM[case.world],
            develop_s=case.develop_s,
            expect_diagnosis=None,
            expect_confirmed=None,
            prepare=lambda lp: rt.driver.set_load(case.rps),
            expect_active=(case.world != "no_fault"),
        )
        if rt.no_elastic:
            # Keep Elastic Cloud off the watch CLI's critical path: the CLI's
            # dotenv loader uses setdefault, so explicit empties win.
            spec["env"] = {"FAULTLINE_ELASTICSEARCH_URL": "", "FAULTLINE_ELASTICSEARCH_API_KEY": ""}
        audit_path = loop.run_world(case.world, "before", spec=spec, incident=incident)
        events = events_for_incident(audit_path, incident)
        diagnosis = diagnosis_from_audit(events)
        verdict = verdict_from_audit(events)
        checks = loop.report.checks
        extra = {"checks_failed": [c.name for c in checks if not c.ok]}
        common = dict(
            diagnosis=diagnosis,
            verdict=verdict,
            wall_s=time.monotonic() - t0,
            started_at=started_at,
            audit_path=str(audit_path),
            extra=extra,
        )
        # The CLI dying before injection is an infrastructure failure, not an answer.
        cli_died = next(
            (c for c in checks if c.name.startswith("watch still waiting for a breach") and not c.ok),
            None,
        )
        if cli_died is not None:
            return [
                _record(case, "active", incident, unscored=True,
                        unscored_reason=f"cli exited before injection ({cli_died.detail})", **common)
            ]
        # A fault that never ignites has no defined expected label; keep the case visible.
        no_ignition = next(
            (c for c in checks if c.name.startswith("incident visible on /stats") and not c.ok),
            None,
        )
        if case.world != "no_fault" and no_ignition is not None:
            return [
                _record(case, "active", incident, unscored=True,
                        unscored_reason="no ignition: the injected fault did not produce an incident on /stats",
                        **common)
            ]
        return [_record(case, "active", incident, **common)]
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - an overnight run must not die on one case
        return [_unscored_record(case, "active", incident, exc, started_at, time.monotonic() - t0)]


def _healthy(world: LiveWorld) -> list[Fingerprint]:
    return world.series(world.start, world.start + timedelta(seconds=60))


def _sandbox_cycle(case: LiveCase, rt: BenchRuntime) -> tuple[LiveWorld, Callable[[], None]]:
    """Shared pre-flight: C5 reset, load level, settle, then start telemetry."""
    rt.driver.reset()
    rt.driver.set_load(case.rps)
    rt.sleep(10)
    return rt.make_world()


def _teardown(rt: BenchRuntime, stop: Callable[[], None] | None) -> None:
    if stop is not None:
        stop()
    try:
        rt.driver.reset()
    except httpx.HTTPError:
        pass


def run_passive_arms(case: LiveCase, rt: BenchRuntime, requested: list[str]) -> list[dict]:
    """One injection scored by the passive LLM arm and/or the centroid arm."""
    incident = _incident_id(case)
    started_at = utcnow()
    t0 = time.monotonic()
    usage: list[dict] = []
    world: LiveWorld | None = None
    stop: Callable[[], None] | None = None
    arms = None
    try:
        world, stop = _sandbox_cycle(case, rt)
        arms = rt.make_arms(usage.append) if "passive" in requested else None
        stash: dict[str, str | None] = {}

        def trigger(w: LiveWorld) -> None:
            rt.driver.inject(case)
            w.develop(case.develop_s)

        def diagnose(fp: Fingerprint) -> str | None:
            if not world.incident_declared():
                stash["passive"] = stash["centroid"] = NO_INCIDENT
                return NO_INCIDENT
            if "centroid" in requested:
                stash["centroid"] = rt.centroid.predict(fp) if rt.centroid is not None else None
            if "passive" in requested:
                stash["passive"] = arms.passive_diagnose(fp, _healthy(world))
            return stash.get("passive") or stash.get("centroid")

        run_passive_only_case(world, trigger, diagnose)
        llm_outputs = dict(arms.last) if arms is not None else None
        records = []
        for arm in requested:
            diagnosis = stash.get(arm)
            records.append(
                _record(
                    case,
                    arm,
                    incident,
                    diagnosis=diagnosis,
                    verdict=None,
                    tokens=_tokens(usage) if arm == "passive" else None,
                    wall_s=time.monotonic() - t0,
                    started_at=started_at,
                    llm_outputs=llm_outputs if arm == "passive" else None,
                )
            )
        return records
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001
        return [
            _unscored_record(case, arm, incident, exc, started_at, time.monotonic() - t0)
            for arm in requested
        ]
    finally:
        _teardown(rt, stop)


def run_llm_only_arm(case: LiveCase, rt: BenchRuntime) -> list[dict]:
    """LLM proposes and interprets an experiment; no math judge."""
    incident = _incident_id(case)
    started_at = utcnow()
    t0 = time.monotonic()
    usage: list[dict] = []
    world: LiveWorld | None = None
    stop: Callable[[], None] | None = None
    arms = None
    try:
        world, stop = _sandbox_cycle(case, rt)
        arms = rt.make_arms(usage.append)
        stash: dict[str, Any] = {}

        def trigger(w: LiveWorld) -> None:
            rt.driver.inject(case)
            w.develop(case.develop_s)

        def choose(incident_fp: Fingerprint, cands: list[Experiment]) -> Experiment | None:
            if not world.incident_declared():
                stash["no_incident"] = True
                return None
            chosen = arms.choose(incident_fp, _healthy(world), cands)
            stash["chosen"] = chosen
            return chosen

        def diagnose(incident_fp: Fingerprint, during: list[Fingerprint], after: list[Fingerprint]) -> str:
            if stash.get("no_incident"):
                return NO_INCIDENT
            return arms.judge(incident_fp, _healthy(world), stash.get("chosen"), during, after)

        result = run_llm_only_case(world, trigger, rt.candidates, choose, diagnose)
        diagnosis = NO_INCIDENT if stash.get("no_incident") else result.diagnosis
        return [
            _record(
                case,
                "llm_only",
                incident,
                diagnosis=diagnosis,
                verdict=_result_verdict(result),
                tokens=_tokens(usage),
                wall_s=time.monotonic() - t0,
                started_at=started_at,
                llm_outputs=dict(arms.last),
            )
        ]
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001
        return [_unscored_record(case, "llm_only", incident, exc, started_at, time.monotonic() - t0)]
    finally:
        _teardown(rt, stop)


def run_random_arm(case: LiveCase, rt: BenchRuntime) -> list[dict]:
    """Random-lever ablation through the measured-experiment path."""
    incident = _incident_id(case)
    started_at = utcnow()
    t0 = time.monotonic()
    world: LiveWorld | None = None
    stop: Callable[[], None] | None = None
    try:
        world, stop = _sandbox_cycle(case, rt)
        stash: dict[str, bool] = {}
        rng = Random(_WORLD_OFFSET[case.world] * 1000 + case.index)

        def trigger(w: LiveWorld) -> None:
            rt.driver.inject(case)
            w.develop(case.develop_s)

        def gated(incident_fp: Fingerprint, items: list[Experiment]) -> Experiment | None:
            if not world.incident_declared():
                stash["no_incident"] = True
                return None
            return rng.choice(items) if items else None

        result = run_hero_case(world, trigger, rt.triage, rt.candidates, chooser=gated)
        diagnosis = NO_INCIDENT if stash.get("no_incident") else result.diagnosis
        return [
            _record(
                case,
                "random",
                incident,
                diagnosis=diagnosis,
                verdict=_result_verdict(result),
                wall_s=time.monotonic() - t0,
                started_at=started_at,
            )
        ]
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001
        return [_unscored_record(case, "random", incident, exc, started_at, time.monotonic() - t0)]
    finally:
        _teardown(rt, stop)


def _aggregate(runs: list[dict]) -> tuple[dict, dict]:
    accuracy: dict[str, dict] = {}
    by_world: dict[str, dict] = {}
    for arm in sorted({r["arm"] for r in runs}):
        arm_runs = [r for r in runs if r["arm"] == arm]
        scored = [r for r in arm_runs if not r["unscored"]]
        correct = sum(1 for r in scored if r["correct"])
        accuracy[arm] = {
            "correct": correct,
            "n": len(scored),
            "accuracy": (correct / len(scored)) if scored else None,
            "unscored": len(arm_runs) - len(scored),
        }
        per_world: dict[str, dict] = {}
        for world in sorted({r["world"] for r in arm_runs}):
            w_runs = [r for r in arm_runs if r["world"] == world]
            w_scored = [r for r in w_runs if not r["unscored"]]
            per_world[world] = {
                "correct": sum(1 for r in w_scored if r["correct"]),
                "n": len(w_scored),
                "unscored": len(w_runs) - len(w_scored),
            }
        by_world[arm] = per_world
    return accuracy, by_world


def _write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, default=str))
    os.replace(tmp, path)


def _arm_sequence(requested: list[str]) -> list[tuple[str, list[str]]]:
    """Ordered arm steps; passive and centroid share one injection per case."""
    sequence: list[tuple[str, list[str]]] = []
    passive_pair = [a for a in ("passive", "centroid") if a in requested]
    for arm in ARMS:
        if arm in ("passive", "centroid"):
            if arm == "passive" and passive_pair:
                sequence.append(("passive_cycle", passive_pair))
        elif arm in requested:
            sequence.append((arm, [arm]))
    return sequence


def _run_step(step: str, sub: list[str], case: LiveCase, rt: BenchRuntime) -> list[dict]:
    if step == "active":
        return run_active_arm(case, rt)
    if step == "passive_cycle":
        return run_passive_arms(case, rt, sub)
    if step == "llm_only":
        return run_llm_only_arm(case, rt)
    return run_random_arm(case, rt)


def _dry_run(plan: list[LiveCase], arms: list[str]) -> int:
    print(f"{'idx':>4}  {'world':<9} {'rps':>4} {'dev_s':>5}  {'expected':<17} params")
    for case in plan:
        print(f"{case.index:>4}  {case.world:<9} {case.rps:>4} {case.develop_s:>5}  {case.expected:<17} {case.params}")
    # centroid shares the passive injection, so it adds no cases of its own
    minutes = sum(_ESTIMATE_MIN[a] for a in arms if a != "centroid") * len(plan)
    print(f"\n{len(plan)} cases x arms {arms}: ~{minutes:.0f} min estimated")
    triage, candidates = load_fixtures()
    print(f"fixtures ok: triage={len(triage.hypotheses)} hypotheses, {len(candidates)} candidate experiments")
    _import_live_loop()
    print("integration/live_loop.py imports ok")
    counts = Counter(label for label, _ in centroid_training_cases())
    print(f"centroid training: {dict(counts) or 'no prior live windows'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--worlds", nargs="+", choices=sorted(GRID), default=sorted(GRID))
    ap.add_argument("--per-world", type=int, default=10)
    ap.add_argument("--cases", type=int, default=None, help="cap the interleaved plan")
    ap.add_argument("--arms", nargs="+", choices=list(ARMS), default=list(ARMS))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--baseline-s", type=int, default=130)
    ap.add_argument("--cli-timeout-s", type=int, default=420)
    ap.add_argument("--openai-model", default="gpt-4.1")
    ap.add_argument("--fault-url", default="http://127.0.0.1:9900")
    ap.add_argument("--control-url", default="http://127.0.0.1:9901")
    ap.add_argument("--stats-urls", nargs="*", default=None,
                    help="telemetry overrides like orders_url=http://... payments_url=... loadgen_url=...")
    ap.add_argument("--no-elastic", action="store_true",
                    help="blank FAULTLINE_ELASTICSEARCH_* for the watch CLI so ES stays off its critical path")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--max-consecutive-unscored", type=int, default=3)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    worlds = args.worlds
    plan = build_plan(worlds, args.per_world, args.cases)

    if args.dry_run:
        return _dry_run(plan, args.arms)

    try:
        httpx.get(f"{args.control_url}/healthz", timeout=3).raise_for_status()
        httpx.get(f"{args.fault_url}/fault/state", timeout=3).raise_for_status()
    except httpx.HTTPError as exc:
        print(f"sandbox not reachable ({exc!r})", file=sys.stderr)
        return 2

    stats_urls = None
    if args.stats_urls:
        stats_urls = dict(item.split("=", 1) for item in args.stats_urls)

    triage, candidates = load_fixtures()
    centroid = fit_centroid_from_runs() if "centroid" in args.arms else None
    centroid_training = dict(Counter(label for label, _ in centroid_training_cases()))

    needs_llm = any(arm in args.arms for arm in ("passive", "llm_only"))
    make_arms = None
    if needs_llm:
        client = make_openai_client()
        if client is None:
            print("OPENAI_API_KEY not set (checked env and repo .env); required for passive/llm_only arms", file=sys.stderr)
            return 2
        from .llm_arms import OpenAIArms

        def make_arms(sink):  # noqa: E731
            return OpenAIArms(client, model=args.openai_model, usage_sink=sink)

    driver = FaultDriver(args.fault_url)
    rt = BenchRuntime(
        driver=driver,
        triage=triage,
        candidates=candidates,
        make_arms=make_arms,
        centroid=centroid,
        make_world=lambda: make_live_world(args.control_url, stats_urls),
        verbose=args.verbose,
        baseline_s=args.baseline_s,
        cli_timeout_s=args.cli_timeout_s,
        no_elastic=args.no_elastic,
    )

    out = args.out or OUT_DIR / f"bench-live-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    report: dict[str, Any] = {
        "schema": "faultline-bench-live/1",
        "started_at": utcnow().isoformat(),
        "finished_at": None,
        "plan": {
            "worlds": worlds,
            "per_world": args.per_world,
            "cap": args.cases,
            "arms": args.arms,
            "baseline_s": args.baseline_s,
            "cli_timeout_s": args.cli_timeout_s,
            "openai_model": args.openai_model,
            "fault_url": args.fault_url,
            "control_url": args.control_url,
            "no_elastic": args.no_elastic,
        },
        "centroid_training": centroid_training,
        "runs": [],
        "accuracy": {},
        "by_world": {},
    }

    sequence = _arm_sequence(args.arms)
    consecutive_unscored = 0
    aborted = None
    try:
        for case in plan:
            if aborted:
                break
            for step, sub in sequence:
                records = _run_step(step, sub, case, rt)
                for record in records:
                    report["runs"].append(record)
                    consecutive_unscored = consecutive_unscored + 1 if record["unscored"] else 0
                    report["accuracy"], report["by_world"] = _aggregate(report["runs"])
                    _write_report(report, out)
                    if consecutive_unscored >= args.max_consecutive_unscored:
                        aborted = f"aborted after {consecutive_unscored} consecutive unscored arm-runs; production appears unhealthy"
                        break
                if aborted:
                    break
    except KeyboardInterrupt:
        aborted = "interrupted"
    report["aborted"] = aborted
    report["finished_at"] = utcnow().isoformat()
    report["accuracy"], report["by_world"] = _aggregate(report["runs"])
    _write_report(report, out)

    print(f"\n{len(report['runs'])} arm-runs -> {out}")
    for arm, stats in report["accuracy"].items():
        acc = stats["accuracy"]
        print(f"  {arm:<9} {stats['correct']}/{stats['n']} correct"
              f"{f' ({acc:.0%})' if acc is not None else ''}, {stats['unscored']} unscored")
    if aborted:
        print(f"  {aborted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
