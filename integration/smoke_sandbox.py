"""End-to-end smoke test of the running sandbox, through its documented interfaces only.

  uv run python smoke_sandbox.py             # full hero sequence (~6 min), PASS/FAIL per check
  uv run python smoke_sandbox.py --quiet     # no per-second rows
  uv run python smoke_sandbox.py --report runs/last.json

Sequence: reset -> healthy baseline -> storm via C5 -> persists after trigger -> retry cap via :9901 ->
heals + ttl expiry + stays healed -> reset -> degraded DB via C5 -> retry cap does not heal, incident returns ->
db failover heals -> reset.

Interfaces used (and nothing else):
  * C5  `faultline_contracts.fault.HttpFaultController` on :9900 (bench-only surface; this is benchmark tooling)
  * C3  control service on :9901, exactly as specified in contracts/README.md and sandbox/INTEGRATION.md
  * `/stats` on orders (:8101), payments (:8102), loadgen (:8103), mapped to C1 keys per sandbox/INTEGRATION.md

The sandbox is treated as frozen: this script only observes and reports; it never tunes anything.
Exit code 1 if any check failed. A JSON report of every check and every 5 s window is written to runs/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from faultline_contracts.fault import CpuStarveFault, DegradeDbFault, HttpFaultController, StormFault, World
from faultline_telemetry.fingerprint import fingerprint_from_stats

STATS_URLS = {
    "orders": os.environ.get("ORDERS_STATS_URL", "http://127.0.0.1:8101"),
    "payments": os.environ.get("PAYMENTS_STATS_URL", "http://127.0.0.1:8102"),
    "loadgen": os.environ.get("LOADGEN_STATS_URL", "http://127.0.0.1:8103"),
}
CONTROL_URL = os.environ.get("CONTROL_URL", "http://127.0.0.1:9901")
FAULT_URL = os.environ.get("FAULT_URL", "http://127.0.0.1:9900")
WINDOW_S = 5
LEVER_KEYS = {"lever_id", "params", "applied_at", "expires_at", "active"}
ALL_LEVERS = {"retry_cap", "shed", "db_failover", "canary_weight"}

Snap = dict[str, dict[str, Any]]


def window_metrics(prev: Snap, cur: Snap) -> dict[str, Any]:
    """One benchmark window using Owner 2's canonical public /stats -> C1 conversion."""
    start = datetime.fromtimestamp(float(prev["orders"]["t"]), tz=timezone.utc)
    end = datetime.fromtimestamp(float(cur["orders"]["t"]), tz=timezone.utc)
    metrics = fingerprint_from_stats(prev, cur, start, end).metrics()
    return {
        "dt_s": float(cur["orders"]["t"]) - float(prev["orders"]["t"]),
        **metrics,
        # gauges (config as the target reports it; used instead of hardcoded thresholds)
        "orders.max_retries": cur["orders"].get("gauges", {}).get("max_retries"),
        "orders.attempt_timeout_ms": cur["orders"].get("gauges", {}).get("attempt_timeout_ms"),
        "payments.db_target": cur["payments"].get("gauges", {}).get("db_target"),
    }


def is_healthy(m: dict[str, Any]) -> bool:
    """Traffic flowing, requests succeed, no amplification, DB p99 under the attempt timeout Orders reports."""
    timeout_ms = m["orders.attempt_timeout_ms"]
    return (
        (m.get("svc.orders.qps") or 0.0) > 0
        and (m.get("svc.orders.error_rate") if m.get("svc.orders.error_rate") is not None else 1.0) <= 0.02
        and (m.get("svc.orders.retry_ratio") or 99.0) <= 1.10
        and timeout_ms is not None
        and m.get("db.query_p99_ms") is not None
        and m["db.query_p99_ms"] < timeout_ms
    )


def is_incident(m: dict[str, Any]) -> bool:
    return (m.get("svc.orders.qps") or 0.0) > 0 and (m.get("svc.orders.error_rate") if m.get("svc.orders.error_rate") is not None else 1.0) >= 0.5


def is_amplified(m: dict[str, Any]) -> bool:
    return (m.get("svc.orders.retry_ratio") or 0.0) > 2.0


def fmt(m: dict[str, Any] | None) -> str:
    if not m:
        return "(no data)"
    keys = ("svc.orders.qps", "svc.orders.retry_ratio", "svc.orders.error_rate", "db.qps", "db.query_p50_ms",
            "db.query_p99_ms", "db.pool_busy_ratio", "orders.max_retries", "payments.db_target")
    parts = []
    for k in keys:
        v = m.get(k)
        parts.append(f"{k.split('.', 1)[1]}={'-' if v is None else (f'{v:.2f}' if isinstance(v, float) else v)}")
    return " ".join(parts)


# -- sampling ---------------------------------------------------------------------------------------

class Sampler:
    """Snapshots /stats once per second; windows are deltas between snapshots."""

    def __init__(self, verbose: bool = True) -> None:
        self.http = httpx.Client(timeout=5.0)
        self.hist: list[tuple[float, Snap]] = []
        self.t0 = time.monotonic()
        self.verbose = verbose

    def snapshot(self) -> Snap:
        return {name: self.http.get(f"{u}/stats").json() for name, u in STATS_URLS.items()}

    def tick(self, label: str = "") -> dict[str, Any] | None:
        self.hist.append((time.monotonic() - self.t0, self.snapshot()))
        m = window_metrics(self.hist[-2][1], self.hist[-1][1]) if len(self.hist) > 1 else None
        if self.verbose and m:
            print(f"  {self.hist[-1][0]:6.0f}s {label:<16} {fmt(m)}", flush=True)
        return m

    def run_for(self, seconds: float, label: str) -> tuple[int, int]:
        """Sample once per second for `seconds`; returns the (first, last) history indices of the span."""
        i0 = len(self.hist)
        self.tick(label)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(max(0.0, min(1.0, end - time.monotonic())))
            self.tick(label)
        return i0, len(self.hist) - 1

    def windows(self, span: tuple[int, int], step: int = WINDOW_S) -> list[dict[str, Any]]:
        i0, i1 = span
        return [window_metrics(self.hist[i][1], self.hist[min(i + step, i1)][1])
                for i in range(i0, i1, step) if min(i + step, i1) > i]

    def tail(self, span: tuple[int, int], seconds: int) -> dict[str, Any]:
        i0, i1 = span
        return window_metrics(self.hist[max(i0, i1 - seconds)][1], self.hist[i1][1])


# -- the smoke run ----------------------------------------------------------------------------------

@dataclass
class Check:
    step: str
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    started_at: str
    checks: list[Check] = field(default_factory=list)
    windows: list[dict[str, Any]] = field(default_factory=list)
    aborted: str | None = None

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


class StepAborted(Exception):
    pass


class Smoke:
    def __init__(self, verbose: bool = True) -> None:
        self.s = Sampler(verbose)
        self.fc = HttpFaultController(FAULT_URL, timeout_s=150)  # reset() blocks until healthy (120 s cap)
        self.ctl = httpx.Client(base_url=CONTROL_URL, timeout=10)
        self.report = Report(started_at=datetime.now(timezone.utc).isoformat())
        self.step = "setup"
        self.default_max_retries: int | None = None

    # helpers
    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.report.checks.append(Check(self.step, name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  [{self.step}] {name}  {detail}", flush=True)
        return bool(ok)

    def begin(self, step: str, title: str) -> None:
        self.step = step
        print(f"\n=== {step}: {title}", flush=True)

    def phase(self, seconds: float, label: str) -> tuple[int, int]:
        span = self.s.run_for(seconds, label)
        for w in self.s.windows(span):
            self.report.windows.append({"phase": label, **w})
        return span

    def levers(self) -> dict[str, Any]:
        r = self.ctl.get("/admin/levers")
        r.raise_for_status()
        return r.json()

    def apply_lever(self, path: str, body: dict[str, Any], lever_id: str) -> dict[str, Any] | None:
        r = self.ctl.post(f"/admin/{path}", json=body)
        ok = r.status_code == 200
        self.check(f"POST /admin/{path} -> 200", ok, r.text[:200])
        if not ok:
            return None
        j = r.json()
        self.check(f"{lever_id} apply response shape", LEVER_KEYS <= set(j) and j["active"] is True
                   and j["lever_id"] == lever_id, json.dumps(j)[:200])
        try:
            exp = datetime.fromisoformat(j["expires_at"].replace("Z", "+00:00"))
            app = datetime.fromisoformat(j["applied_at"].replace("Z", "+00:00"))
            self.check(f"{lever_id} expires_at ~ applied_at + ttl_s", abs((exp - app).total_seconds() - body["ttl_s"]) <= 1.5,
                       f"applied_at={j['applied_at']} expires_at={j['expires_at']}")
        except (KeyError, ValueError, AttributeError) as e:
            self.check(f"{lever_id} timestamps parse as ISO-8601", False, repr(e))
        st = self.levers().get(lever_id, {})
        self.check(f"GET /admin/levers shows {lever_id} active", st.get("active") is True, json.dumps(st)[:200])
        return j

    def wait_lever_expired(self, lever_id: str, expires_at: str | None, grace_s: float = 10.0) -> bool:
        """Wait until the control service reports the lever inactive; deadline = expires_at + grace."""
        if expires_at:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            deadline = time.monotonic() + max(0.0, (exp - datetime.now(timezone.utc)).total_seconds()) + grace_s
        else:
            deadline = time.monotonic() + grace_s
        t0 = time.monotonic()
        while time.monotonic() < deadline:
            self.s.tick("waiting for ttl")
            if not self.levers()[lever_id]["active"]:
                return self.check(f"{lever_id} ttl auto-reverts (control reports inactive)", True,
                                  f"{time.monotonic() - t0:.1f}s after start of wait")
            time.sleep(1)
        return self.check(f"{lever_id} ttl auto-reverts (control reports inactive)", False,
                          f"still active {grace_s:.0f}s past expires_at={expires_at}")

    def reset(self, label: str) -> None:
        t0 = time.monotonic()
        try:
            st = self.fc.reset()
        except httpx.HTTPStatusError as e:
            self.check(f"{label}: C5 reset returns 200 (healthy)", False, f"{e.response.status_code} {e.response.text[:200]}")
            raise StepAborted("reset returned an error; infrastructure failure, not scoring further")
        except httpx.HTTPError as e:
            self.check(f"{label}: C5 reset reachable", False, repr(e))
            raise StepAborted("fault controller unreachable")
        self.check(f"{label}: C5 reset returns world=none", st.world == World.none and not st.active,
                   f"{time.monotonic() - t0:.1f}s; state={st.model_dump_json()}")
        lv = self.levers()
        self.check(f"{label}: all levers inactive after reset", set(lv) == ALL_LEVERS and not any(v["active"] for v in lv.values()),
                   json.dumps(lv)[:300])

    def assert_all_healthy(self, name: str, span: tuple[int, int]) -> bool:
        ws = self.s.windows(span)
        bad = [w for w in ws if not is_healthy(w)]
        return self.check(name, bool(ws) and not bad, f"{len(ws) - len(bad)}/{len(ws)} windows healthy; last: {fmt(ws[-1] if ws else None)}")

    # steps
    def step_baseline(self, seconds: int = 15) -> None:
        self.begin("1-baseline", f"reset, then {seconds}s of healthy equilibrium")
        self.reset("baseline")
        span = self.phase(seconds, "baseline")
        self.assert_all_healthy("healthy baseline (every 5s window healthy)", span)
        m = self.s.tail(span, seconds)
        self.default_max_retries = m["orders.max_retries"]
        self.check("orders reports its retry policy", m["orders.max_retries"] is not None and m["orders.attempt_timeout_ms"] is not None,
                   f"max_retries={m['orders.max_retries']} attempt_timeout_ms={m['orders.attempt_timeout_ms']}")
        self.check("payments targets primary DB", m["payments.db_target"] == "primary", f"db_target={m['payments.db_target']}")

    def step_storm(self, delay_ms: int = 800, duration_s: int = 20, persist_s: int = 30) -> None:
        self.begin("2-storm", f"C5 storm {delay_ms}ms x {duration_s}s")
        st = self.fc.storm(StormFault(delay_ms=delay_ms, duration_s=duration_s))
        self.check("C5 storm returns world=storm active=True", st.world == World.storm and st.active, st.model_dump_json())
        self.phase(duration_s, "trigger")
        self.begin("3-storm-persists", f"trigger gone; wait 10s, then observe {persist_s}s")
        self.phase(10, "settle")
        st = self.fc.state()
        self.check("C5 state: trigger inactive after duration_s", st.world == World.storm and not st.active, st.model_dump_json())
        span = self.phase(persist_s, "trigger gone")
        ws = self.s.windows(span)
        stuck = [w for w in ws if is_incident(w) and is_amplified(w)]
        self.check(f"storm persists {persist_s}s after trigger (every window: error_rate>=0.5 and retry_ratio>2)",
                   bool(ws) and len(stuck) == len(ws), f"{len(stuck)}/{len(ws)} windows; {fmt(self.s.tail(span, persist_s))}")
        m = self.s.tail(span, persist_s)
        self.check("storm: DB pool saturated", (m["db.pool_busy_ratio"] or 0) >= 0.95, f"pool_busy_ratio={m['db.pool_busy_ratio']}")

    def step_storm_cap(self, cap_s: int = 20, after_s: int = 30) -> None:
        self.begin("4-retry-cap", f"POST /admin/retry_override max_retries=0 ttl={cap_s}s")
        j = self.apply_lever("retry_override", {"max_retries": 0, "ttl_s": cap_s}, "retry_cap")
        cap = self.phase(cap_s - 2, "retry cap 0")
        self.check("retry cap reaches Orders (gauge max_retries=0)", self.s.tail(cap, 1)["orders.max_retries"] == 0,
                   f"max_retries={self.s.tail(cap, 1)['orders.max_retries']}")
        self.begin("5-recovery", "storm heals under cap; ttl expires; stays healed")
        during = self.s.tail(cap, 5)
        self.check("storm heals under retry cap (last 5s healthy)", is_healthy(during), fmt(during))
        self.wait_lever_expired("retry_cap", j["expires_at"] if j else None)
        m = self.s.tick("after ttl")
        self.check("Orders max_retries back to default after ttl", m is not None and m["orders.max_retries"] == self.default_max_retries,
                   f"max_retries={m['orders.max_retries'] if m else None} default={self.default_max_retries}")
        st = self.levers()["retry_cap"]
        self.check("expired lever keeps last params (status -> expired)", st.get("params", {}).get("max_retries") == 0
                   and st.get("expires_at") is not None, json.dumps(st)[:200])
        span = self.phase(after_s, "cap released")
        self.assert_all_healthy(f"stays healed {after_s}s after cap expiry (storm was the sustaining cause)", span)

    def step_reset_mid(self) -> None:
        self.begin("6-reset", "C5 reset between worlds")
        self.reset("mid")

    def step_degraded(self, capacity_qps: float = 40, develop_s: int = 30) -> None:
        self.begin("7-degraded-db", f"C5 degrade_db capacity={capacity_qps}")
        self.assert_all_healthy("healthy before degrade", self.phase(10, "baseline"))
        st = self.fc.degrade_db(DegradeDbFault(capacity_qps=capacity_qps))
        self.check("C5 degrade_db returns world=degraded_db active=True", st.world == World.degraded_db and st.active, st.model_dump_json())
        inc = self.phase(develop_s, "incident")
        m = self.s.tail(inc, 15)
        self.check("degraded: incident develops (error_rate>=0.5, retry_ratio>2)", is_incident(m) and is_amplified(m), fmt(m))

    def step_degraded_cap(self, cap_s: int = 20, after_s: int = 30) -> None:
        self.begin("8-retry-cap-degraded", f"retry cap 0 for {cap_s}s must not permanently heal")
        j = self.apply_lever("retry_override", {"max_retries": 0, "ttl_s": cap_s}, "retry_cap")
        cap = self.phase(cap_s - 2, "retry cap 0")
        during = self.s.tail(cap, 10)
        self.check("degraded: cap removes amplification (retry_ratio<=1.1)", (during["svc.orders.retry_ratio"] or 9) <= 1.1, fmt(during))
        self.check("degraded: cap does NOT heal (still unhealthy while capped)", not is_healthy(during), fmt(during))
        self.wait_lever_expired("retry_cap", j["expires_at"] if j else None)
        after = self.phase(after_s, "cap released")
        a = self.s.tail(after, 15)
        self.check("degraded: incident returns after cap expiry", is_incident(a) and is_amplified(a), fmt(a))

    def step_failover(self, ttl_s: int = 60, watch_s: int = 30) -> None:
        self.begin("9-db-failover", f"POST /admin/db/failover ttl={ttl_s}s")
        self.apply_lever("db/failover", {"ttl_s": ttl_s}, "db_failover")
        fo = self.phase(watch_s, "db failover")
        self.check("failover reaches Payments (gauge db_target=standby)", self.s.tail(fo, 1)["payments.db_target"] == "standby",
                   f"db_target={self.s.tail(fo, 1)['payments.db_target']}")
        m = self.s.tail(fo, 15)
        self.check("degraded: db failover heals (last 15s healthy)", is_healthy(m), fmt(m))

    def step_reset_end(self) -> None:
        self.begin("10-reset", "C5 reset after degraded DB (clears failover lever too)")
        self.reset("final")
        m = self.s.tick("after reset")
        self.check("payments back on primary after reset", m is not None and m["payments.db_target"] == "primary",
                   f"db_target={m['payments.db_target'] if m else None}")
        span = self.phase(15, "after reset")
        self.assert_all_healthy("healthy baseline restored after final reset", span)

    def step_cpu(self, cpus: float = 0.1, cap_s: int = 20) -> None:
        """None-of-the-above world: neither the retry cap nor failover heals it. Optional (needs the Docker socket)."""
        self.begin("11-cpu-starve", f"C5 cpu_starve payments cpus={cpus}: neither lever should heal")
        self.assert_all_healthy("healthy before cpu_starve", self.phase(10, "baseline"))
        try:
            st = self.fc.cpu_starve(CpuStarveFault(service="payments", cpus=cpus))
        except httpx.HTTPStatusError as e:
            self.check("C5 cpu_starve accepted", False, f"{e.response.status_code} {e.response.text[:160]} "
                       "(501 = Docker socket not mounted; documented, skipping world)")
            return
        self.check("C5 cpu_starve returns world=cpu_starve active=True", st.world == World.cpu_starve and st.active, st.model_dump_json())
        inc = self.phase(30, "incident")
        self.check("cpu: incident develops", is_incident(self.s.tail(inc, 15)), fmt(self.s.tail(inc, 15)))
        j = self.apply_lever("retry_override", {"max_retries": 0, "ttl_s": cap_s}, "retry_cap")
        cap = self.phase(cap_s - 2, "retry cap 0")
        self.wait_lever_expired("retry_cap", j["expires_at"] if j else None)
        after = self.phase(30, "cap released")
        self.check("cpu: retry cap does not permanently heal", not is_healthy(self.s.tail(after, 15)),
                   f"during: {fmt(self.s.tail(cap, 10))} | after: {fmt(self.s.tail(after, 15))}")
        self.apply_lever("db/failover", {"ttl_s": 60}, "db_failover")
        fo = self.phase(30, "db failover")
        self.check("cpu: db failover does not heal", not is_healthy(self.s.tail(fo, 15)), fmt(self.s.tail(fo, 15)))
        self.begin("12-reset", "C5 reset after cpu_starve")
        self.reset("cpu")
        self.assert_all_healthy("healthy baseline restored after cpu reset", self.phase(15, "after reset"))

    def run(self, with_cpu: bool = False) -> Report:
        steps = [self.step_baseline, self.step_storm, self.step_storm_cap, self.step_reset_mid, self.step_degraded,
                 self.step_degraded_cap, self.step_failover, self.step_reset_end] + ([self.step_cpu] if with_cpu else [])
        try:
            for st in steps:
                st()
        except StepAborted as e:
            self.report.aborted = str(e)
            print(f"\nABORTED: {e}", flush=True)
        except httpx.HTTPError as e:
            self.report.aborted = f"{self.step}: {e!r}"
            self.check("no transport errors", False, repr(e))
            print(f"\nABORTED in {self.step}: {e!r}", flush=True)
        finally:
            if self.report.aborted:
                try:
                    self.fc.reset()
                except httpx.HTTPError:
                    pass
        return self.report


def write_report(report: Report, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(report), indent=1, default=str))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true", help="suppress per-second rows")
    ap.add_argument("--report", type=Path, default=None, help="JSON report path (default runs/smoke-<utc>.json)")
    ap.add_argument("--cpu", action="store_true", help="also run the cpu_starve (none-of-the-above) world")
    ap.add_argument("--repeat", type=int, default=1, help="run the whole sequence N times; summary of pass counts and timings")
    args = ap.parse_args()
    try:
        httpx.get(f"{CONTROL_URL}/healthz", timeout=3).raise_for_status()
        httpx.get(f"{FAULT_URL}/fault/state", timeout=3).raise_for_status()
    except httpx.HTTPError as e:
        print(f"sandbox not reachable ({e!r}); start it with `cd sandbox && docker compose up -d --build`")
        sys.exit(2)
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    any_failed = False
    for i in range(args.repeat):
        if args.repeat > 1:
            print(f"\n##### run {i + 1}/{args.repeat}", flush=True)
        report = Smoke(verbose=not args.quiet).run(with_cpu=args.cpu)
        out = args.report if args.repeat == 1 and args.report else Path(__file__).parent / "runs" / f"smoke-{stamp}-{i + 1}.json"
        write_report(report, out)
        failed = report.failed
        any_failed |= bool(failed or report.aborted)
        print(f"\n{len(report.checks) - len(failed)}/{len(report.checks)} checks passed; report: {out}")
        for c in failed:
            print(f"  FAILED [{c.step}] {c.name}  {c.detail}")
    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
