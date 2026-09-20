"""Run the real Faultline v5 loop against the live sandbox, one hidden world at a time.

  uv run python live_loop.py storm                 # inject World A, expect H_meta confirmed
  uv run python live_loop.py degraded              # inject World B, expect anything but a confirmed H_meta
  uv run python live_loop.py storm degraded        # both, back to back
  uv run python live_loop.py storm --start-watch after   # operator arrives late: no healthy baseline

Per world: C5 reset -> start `faultline watch --telemetry sandbox --levers sandbox --brain live` (it polls
/stats and waits for the checkout SLO breach) -> let it accrue a healthy baseline -> inject the fault via C5 ->
wait for the CLI to exit -> read the C4 audit log it wrote -> check the verdict, that every lever apply has a
matching undo (or expired on the target), and that the sandbox is healthy again -> C5 reset.

This is bench-side tooling: it drives C5 and reads the audit log; Faultline itself only sees C1 + C3.
The sandbox is frozen; only integration bugs are reported.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from faultline_contracts import AuditEvent, EventKind, experiment_windows
from faultline_contracts.fault import DegradeDbFault, HttpFaultController, StormFault, World

from smoke_sandbox import CONTROL_URL, FAULT_URL, Check, Report, Sampler, StepAborted, fmt, is_healthy, is_incident

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = ROOT / "product"
RUNS = Path(__file__).parent / "runs"

WORLDS = {
    "storm": dict(inject=lambda fc: fc.storm(StormFault(delay_ms=800, duration_s=20)), world=World.storm,
                  develop_s=35, expect_diagnosis="H_meta", expect_confirmed=True),
    "degraded": dict(inject=lambda fc: fc.degrade_db(DegradeDbFault(capacity_qps=40)), world=World.degraded_db,
                     develop_s=15, expect_diagnosis=None, expect_confirmed=None),
}


class LiveLoop:
    def __init__(self, verbose: bool, baseline_s: int, cli_timeout_s: int,
                 lab_url: str | None = None, max_clones: int = 1, investigate_budget: int = 3) -> None:
        self.s = Sampler(verbose)
        self.fc = HttpFaultController(FAULT_URL, timeout_s=150)
        self.ctl = httpx.Client(base_url=CONTROL_URL, timeout=10)
        self.report = Report(started_at=datetime.now(timezone.utc).isoformat())
        self.baseline_s, self.cli_timeout_s = baseline_s, cli_timeout_s
        self.lab_url, self.max_clones, self.investigate_budget = lab_url, max_clones, investigate_budget
        self.step = "setup"

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.report.checks.append(Check(self.step, name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  [{self.step}] {name}  {detail}", flush=True)
        return bool(ok)

    def phase(self, seconds: float, label: str) -> tuple[int, int]:
        span = self.s.run_for(seconds, label)
        for w in self.s.windows(span):
            self.report.windows.append({"phase": f"{self.step}/{label}", **w})
        return span

    def quench(self, label: str, attempts: int = 2) -> bool:
        """Break a storm that outlived its trigger, so the next case starts clean.

        Clearing the C5 fault removes the *trigger*; a retry storm that has become
        self-sustaining keeps running without it -- that is the whole premise of the
        hero scenario. A single post-reset health sample can also land in a brief
        lull and read healthy while the storm re-establishes, which is how a case
        ends up measuring its noise model against a stormed baseline.

        So: if the system is not healthy after the reset, cap retries to collapse the
        amplification, release the cap, and require health to *hold* afterwards. The
        cap is released before the caller's baseline begins; a cap left on would make
        the baseline look healthy without it being so.
        """
        for attempt in range(attempts):
            if is_healthy(self.s.tail(self.phase(10, f"{label} settle"), 8)):
                return True
            self.ctl.post("/admin/retry_override", json={"max_retries": 0, "ttl_s": 40})
            self.phase(25, f"{label} quench")
            self.ctl.delete("/admin/retry_override")
            if is_healthy(self.s.tail(self.phase(20, f"{label} quench release"), 10)):
                self.check(f"{label}: storm quenched (attempt {attempt + 1})", True)
                return True
        return False

    def reset(self, label: str) -> None:
        try:
            st = self.fc.reset()
        except httpx.HTTPError as e:
            self.check(f"{label}: C5 reset healthy", False, repr(e))
            raise StepAborted("reset failed; infrastructure problem")
        lv = self.ctl.get("/admin/levers").json()
        self.check(f"{label}: C5 reset healthy, levers inactive", st.world == World.none and not any(v["active"] for v in lv.values()),
                   json.dumps({k: v["active"] for k, v in lv.items()}))
        if not self.quench(label):
            self.check(f"{label}: sandbox healthy before next phase", False, fmt(self.s.tail(self.phase(5, f"{label} final"), 5)))

    # -- one world -------------------------------------------------------------------------------
    def run_world(self, name: str, start_watch: str, *, spec: dict | None = None, incident: str | None = None) -> Path:
        spec = spec or WORLDS[name]
        self.step = f"{name}"
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        incident = incident or f"live-{name}-{stamp}"
        audit_path = RUNS / f"audit-{incident}.jsonl"
        cli_log = RUNS / f"cli-{incident}.log"
        print(f"\n=== {name}: incident {incident}; watch starts {start_watch} injection", flush=True)
        self.reset("pre")
        if spec.get("prepare"):
            spec["prepare"](self)

        proc: subprocess.Popen | None = None

        def start_cli() -> subprocess.Popen:
            uv_run = ["uv", "run"] + (["--extra", "llm"] if self.lab_url else [])
            cmd = uv_run + ["faultline", "--audit-log", str(audit_path), "watch", "--incident", incident,
                   "--telemetry", "sandbox", "--levers", "sandbox", "--brain", "live",
                   "--detect-timeout", str(self.baseline_s + spec["develop_s"] + 120)]
            if self.lab_url:
                cmd += ["--lab-url", self.lab_url, "--max-clones", str(self.max_clones),
                        "--investigate-budget", str(self.investigate_budget)]
            env = {**os.environ, **spec.get("env", {})}
            log = open(cli_log, "w")
            p = subprocess.Popen(cmd, cwd=PRODUCT, stdout=log, stderr=subprocess.STDOUT, text=True, env=env)
            print(f"  started: {' '.join(cmd[2:])}  (log {cli_log.name})", flush=True)
            return p

        if start_watch == "before":
            proc = start_cli()
            span = self.phase(self.baseline_s, "baseline (watch running)")
            ws = self.s.windows(span)
            self.check("healthy baseline while watch accrues telemetry", all(is_healthy(w) for w in ws),
                       f"{sum(is_healthy(w) for w in ws)}/{len(ws)} healthy")
            self.check("watch still waiting for a breach (no false detect on healthy traffic)", proc.poll() is None,
                       f"exit={proc.poll()}")
        st = spec["inject"](self.fc)
        inject_at = datetime.now(timezone.utc)
        self.check(f"C5 inject -> world={spec['world'].value}", st.world == spec["world"] and (st.active or not spec.get("expect_active", True)), st.model_dump_json())
        span = self.phase(spec["develop_s"], "incident develops")
        self.check("incident visible on /stats", is_incident(self.s.tail(span, 10)), fmt(self.s.tail(span, 10)))
        if start_watch == "after":
            proc = start_cli()

        # wait for the CLI while sampling
        t0 = time.monotonic()
        while proc.poll() is None and time.monotonic() - t0 < self.cli_timeout_s:
            self.phase(5, "faultline running")
        if proc.poll() is None:
            proc.kill()
            self.check("faultline watch exits within timeout", False, f"killed after {self.cli_timeout_s}s")
        else:
            self.check("faultline watch exits 0", proc.returncode == 0, f"exit={proc.returncode} in {time.monotonic() - t0:.0f}s")
        cli_tail = cli_log.read_text().strip().splitlines()[-12:]
        print("  --- faultline output (tail) ---\n  " + "\n  ".join(cli_tail), flush=True)

        self.check_audit(incident, audit_path, inject_at, spec)
        span = self.phase(15, "after faultline")
        after = self.s.tail(span, 10)
        lv = self.ctl.get("/admin/levers").json()
        active = [k for k, v in lv.items() if v["active"]]
        print(f"  levers after run: {json.dumps(lv)[:300]}", flush=True)
        self.report.windows.append({"phase": f"{self.step}/levers_after", "active": active})
        if name == "storm":
            self.check("storm: system healthy after Faultline (mitigation or healed loop)", is_healthy(after), fmt(after))
        self.reset("post")
        self.check("post-reset: healthy", is_healthy(self.s.tail(self.phase(10, "post reset"), 8)))
        return audit_path

    def check_audit(self, incident: str, path: Path, inject_at: datetime, spec: dict) -> None:
        if not path.exists():
            self.check("audit log written", False, str(path))
            return
        events = [AuditEvent.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]
        events = [e for e in events if e.incident_id == incident]
        kinds = [e.kind for e in events]
        self.check("audit log written for incident", bool(events), f"{len(events)} events: {[k.value for k in kinds]}")
        if not events:
            return
        detect = next((e for e in events if e.kind == EventKind.detect), None)
        self.check("detect event present, after injection", detect is not None and detect.ts >= inject_at,
                   f"detect ts={detect.ts if detect else None} inject_at={inject_at.isoformat()}")
        if detect:
            self.report.windows.append({"phase": f"{self.step}/timing", "time_to_detect_s": (detect.ts - inject_at).total_seconds()})
        self.check("triage event present", EventKind.triage in kinds)
        wins = experiment_windows(events)
        self.check("experiment_start/experiment_end pair recorded", len(wins) >= 1,
                   f"{[(w.experiment_id, (w.release - w.start).total_seconds()) for w in wins]}")
        applies = [e for e in events if e.kind == EventKind.action_apply]
        undos = {e.action_id for e in events if e.kind == EventKind.action_undo}
        unmatched = [e for e in applies if e.action_id not in undos]
        mitigation_ids = {e.action_id for e in events if e.kind == EventKind.mitigation and e.action_id}
        self.check("every action_apply has an action_undo (or is the kept mitigation)",
                   all(e.action_id in mitigation_ids for e in unmatched),
                   f"{len(applies)} applies, {len(undos)} undos, unmatched={[e.summary for e in unmatched]}")
        self.check("action budget respected (<=5 applies)", len(applies) <= 5, f"{len(applies)}")
        verdict = next((e for e in events if e.kind == EventKind.verdict), None)
        self.check("verdict event present (actor=math)", verdict is not None and verdict.actor.value == "math",
                   verdict.summary if verdict else "none")
        if verdict:
            diag, conf = verdict.payload.get("diagnosis"), verdict.payload.get("confirmed")
            obs = verdict.payload.get("observations", [])
            print("  --- verdict observations ---", flush=True)
            for o in obs[:12]:
                print(f"  {o.get('metric'):<28} phase={o.get('phase', '?'):<13} base={o.get('baseline')} meas={o.get('measured')} "
                      f"z={o.get('z')} dir={o.get('direction')}", flush=True)
            self.report.windows.append({"phase": f"{self.step}/verdict", "diagnosis": diag, "confirmed": conf, "observations": obs})
            if spec["expect_diagnosis"] is not None:
                self.check(f"verdict diagnosis == {spec['expect_diagnosis']} confirmed={spec['expect_confirmed']}",
                           diag == spec["expect_diagnosis"] and conf == spec["expect_confirmed"], f"got {diag} confirmed={conf}")
            else:
                self.check("verdict does not confirm H_meta on the degraded world", not (diag == "H_meta" and conf),
                           f"got {diag} confirmed={conf}")
        self.check("run ends in report or page_human", kinds[-1] in (EventKind.report, EventKind.page_human), kinds[-1].value)
        refused = [e.summary for e in events if e.kind == EventKind.refused]
        if refused:
            print(f"  refusals: {refused}", flush=True)

    def run(self, worlds: list[str], start_watch: str) -> Report:
        try:
            for w in worlds:
                self.run_world(w, start_watch)
        except StepAborted as e:
            self.report.aborted = str(e)
        except httpx.HTTPError as e:
            self.report.aborted = f"{self.step}: {e!r}"
            self.check("no transport errors", False, repr(e))
        finally:
            if self.report.aborted:
                try:
                    self.fc.reset()
                except httpx.HTTPError:
                    pass
        return self.report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("worlds", nargs="+", choices=sorted(WORLDS))
    ap.add_argument("--start-watch", choices=("before", "after"), default="before",
                    help="start `faultline watch` before injection (continuous watch) or after (late operator)")
    ap.add_argument("--baseline-s", type=int, default=130, help="healthy seconds to let watch accrue (orchestrator BASELINE_S=120)")
    ap.add_argument("--cli-timeout-s", type=int, default=420)
    ap.add_argument("--lab-url", help="C6 clone manager; enables clone investigation in the watch run")
    ap.add_argument("--max-clones", type=int, default=1)
    ap.add_argument("--investigate-budget", type=int, default=3)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    try:
        httpx.get(f"{CONTROL_URL}/healthz", timeout=3).raise_for_status()
        httpx.get(f"{FAULT_URL}/fault/state", timeout=3).raise_for_status()
    except httpx.HTTPError as e:
        print(f"sandbox not reachable ({e!r})")
        sys.exit(2)
    RUNS.mkdir(exist_ok=True)
    report = LiveLoop(not args.quiet, args.baseline_s, args.cli_timeout_s,
                      lab_url=args.lab_url, max_clones=args.max_clones,
                      investigate_budget=args.investigate_budget).run(args.worlds, args.start_watch)
    out = RUNS / f"live-{'-'.join(args.worlds)}-{args.start_watch}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    from smoke_sandbox import write_report
    write_report(report, out)
    failed = report.failed
    print(f"\n{len(report.checks) - len(failed)}/{len(report.checks)} checks passed; report: {out}")
    for c in failed:
        print(f"  FAILED [{c.step}] {c.name}  {c.detail}")
    sys.exit(1 if failed or report.aborted else 0)


if __name__ == "__main__":
    main()
