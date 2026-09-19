"""Scripted checks of the sandbox's causal behavior against a running stack.

  uv run python scripts/validate.py baseline     # healthy equilibrium is stable
  uv run python scripts/validate.py storm        # World A: persists after trigger; cap heals; stays healed
  uv run python scripts/validate.py degraded     # World B: cap doesn't heal; returns; failover heals
  uv run python scripts/validate.py cpu          # none-of-the-above: neither cap nor failover heals
  uv run python scripts/validate.py levers       # control API: validation, ttl auto-revert, shed, canary
  uv run python scripts/validate.py reset        # reset from a live storm returns to baseline
  uv run python scripts/validate.py repeat -n 5  # N back-to-back storm scenarios
  uv run python scripts/validate.py all

Prints one diagnostics row per second (with hidden fault state, since this is benchmark
tooling) and a PASS/FAIL line per check. Exit code 1 if any check failed.
"""

import argparse
import sys
import time
from typing import Any, Callable

import httpx

from _common import CONTROL_URL, FAULT_URL, GATEWAY_URL, Sampler, probe
from faultline_contracts.fault import CpuStarveFault, DegradeDbFault, HttpFaultController, StormFault, World

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    return ok


def fmt(r: dict[str, Any] | None) -> str:
    if not r:
        return "(no data)"
    keys = ("logical_qps", "retry_ratio", "ok_ratio", "db_issued_qps", "db_completed_qps", "db_p99_ms",
            "pool_busy_ratio", "late_completions_qps")
    return " ".join(f"{k}={r[k]:.2f}" if isinstance(r[k], float) else f"{k}={r[k]}" for k in keys)


class Run:
    def __init__(self) -> None:
        self.s = Sampler(show_hidden=True)
        self.fc = HttpFaultController(FAULT_URL, timeout_s=300)
        self.ctl = httpx.Client(base_url=CONTROL_URL, timeout=10)
        cfg = httpx.get(f"{FAULT_URL}/debug/config", timeout=5).json()
        self.timeout_ms = cfg["attempt_timeout_ms"]
        self.capacity = cfg["primary_capacity_qps_nominal"]

    # -- helpers --------------------------------------------------------------------------
    def phase(self, seconds: float, label: str) -> tuple[int, int]:
        i0 = len(self.s.hist)
        self.s.run_for(seconds, label)
        return i0, len(self.s.hist) - 1

    def windows(self, span: tuple[int, int], step: int = 5) -> list[dict[str, Any]]:
        i0, i1 = span
        h = self.s.hist
        return [probe.row(h[i][1], h[min(i + step, i1)][1]) for i in range(max(i0 - 1, 0), i1, step)
                if min(i + step, i1) > i]

    def tail(self, span: tuple[int, int], seconds: int) -> dict[str, Any]:
        i0, i1 = span
        return probe.row(self.s.hist[max(i0, i1 - seconds)][1], self.s.hist[i1][1])

    def healthy(self, r: dict[str, Any]) -> bool:
        return probe.is_healthy(r, self.timeout_ms)

    def reset(self) -> bool:
        t0 = time.monotonic()
        try:
            st = self.fc.reset()
        except httpx.HTTPError as e:
            return check("reset returns healthy", False, str(e))
        self.s.tick("after reset")
        return check("reset returns healthy", st.world == World.none, f"{time.monotonic() - t0:.1f}s")

    def lever(self, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return self.ctl.post(f"/admin/{path}", json=body) if body is not None else self.ctl.delete(f"/admin/{path}")

    def wait_lever_expired(self, lever_id: str, timeout_s: float) -> bool:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            self.s.tick("waiting for ttl")
            if not self.ctl.get("/admin/levers").json()[lever_id]["active"]:
                return True
            time.sleep(1)
        return False

    def baseline(self, seconds: int = 15) -> bool:
        span = self.phase(seconds, "baseline")
        return check("baseline healthy before injecting", self.healthy(self.tail(span, seconds - 2)),
                     fmt(self.tail(span, seconds - 2)))

    # -- scenarios --------------------------------------------------------------------------
    def scenario_baseline(self, seconds: int = 120) -> None:
        print(f"\n=== baseline: {seconds}s of healthy equilibrium")
        self.reset()
        span = self.phase(seconds, "baseline")
        ws = self.windows(span)
        bad = [w for w in ws if not self.healthy(w)]
        check("baseline stable (every 5s window healthy)", not bad,
              f"{len(ws) - len(bad)}/{len(ws)} healthy; overall {fmt(self.tail(span, seconds))}")

    def scenario_storm(self, delay_ms: int = 800, duration_s: int = 20, persist_s: int = 60,
                       cap_s: int = 20, after_s: int = 60, tag: str = "") -> bool:
        print(f"\n=== storm{tag}: trigger {delay_ms}ms x {duration_s}s, then watch {persist_s}s")
        n0 = len(results)
        self.reset()
        self.baseline()
        self.fc.storm(StormFault(delay_ms=delay_ms, duration_s=duration_s))
        self.phase(duration_s, "trigger")
        post = self.phase(persist_s, "trigger gone")
        ws = self.windows(post)
        stuck = [w for w in ws if probe.is_incident(w) and (w["retry_ratio"] or 0) > 2.0]
        check(f"storm{tag} persists {persist_s}s after trigger ends", len(stuck) == len(ws),
              f"{len(stuck)}/{len(ws)} windows unhealthy+amplified; {fmt(self.tail(post, persist_s))}")
        overall = self.tail(post, persist_s)
        check(f"storm{tag} abandoned work keeps DB busy", overall["db_completed_qps"] >= 0.7 * self.capacity
              and overall["late_completions_qps"] > 0,
              f"db_completed={overall['db_completed_qps']:.0f}/s (nominal cap {self.capacity:.0f}), "
              f"late={overall['late_completions_qps']:.0f}/s")
        r = self.lever("retry_override", {"max_retries": 0, "ttl_s": cap_s})
        check(f"storm{tag} retry cap accepted", r.status_code == 200, r.text[:200])
        cap = self.phase(cap_s - 1, "retry cap 0")
        check(f"storm{tag} retry cap heals", self.healthy(self.tail(cap, 5)), fmt(self.tail(cap, 5)))
        check(f"storm{tag} cap ttl auto-reverts", self.wait_lever_expired("retry_cap", 10))
        after = self.phase(after_s, "cap released")
        ws = self.windows(after)
        bad = [w for w in ws if not self.healthy(w)]
        check(f"storm{tag} stays healed {after_s}s after cap expires", not bad and
              self.tail(after, after_s)["max_retries"] == 3,
              f"{len(ws) - len(bad)}/{len(ws)} healthy; {fmt(self.tail(after, after_s))}")
        return all(ok for _, ok, _ in results[n0:])

    def scenario_degraded(self, capacity_qps: float = 40, cap_s: int = 20) -> None:
        print(f"\n=== degraded DB: capacity {capacity_qps} qps")
        self.reset()
        self.baseline()
        self.fc.degrade_db(DegradeDbFault(capacity_qps=capacity_qps))
        inc = self.phase(30, "incident")
        check("degraded: incident develops", probe.is_incident(self.tail(inc, 15)), fmt(self.tail(inc, 15)))
        self.lever("retry_override", {"max_retries": 0, "ttl_s": cap_s})
        cap = self.phase(cap_s - 1, "retry cap 0")
        during = self.tail(cap, 10)
        check("degraded: retry cap removes amplification", (during["retry_ratio"] or 9) <= 1.1, fmt(during))
        check("degraded: retry cap does NOT heal", not self.healthy(during), fmt(during))
        self.wait_lever_expired("retry_cap", 10)
        after = self.phase(30, "cap released")
        a = self.tail(after, 15)
        check("degraded: incident persists after cap expires", probe.is_incident(a) and (a["retry_ratio"] or 0) > 2,
              fmt(a))
        r = self.lever("db/failover", {"ttl_s": 60})
        check("degraded: failover accepted", r.status_code == 200, r.text[:200])
        fo = self.phase(30, "db failover")
        check("degraded: db failover heals", self.healthy(self.tail(fo, 15)), fmt(self.tail(fo, 15)))
        self.reset()

    def scenario_cpu(self, cpus: float = 0.1) -> None:
        print(f"\n=== cpu starvation of payments: {cpus} cpus")
        self.reset()
        self.baseline()
        try:
            self.fc.cpu_starve(CpuStarveFault(service="payments", cpus=cpus))
        except httpx.HTTPError as e:
            check("cpu: starve applied", False, str(e))
            return
        inc = self.phase(30, "incident")
        check("cpu: incident develops", probe.is_incident(self.tail(inc, 15)), fmt(self.tail(inc, 15)))
        self.lever("retry_override", {"max_retries": 0, "ttl_s": 20})
        cap = self.phase(19, "retry cap 0")
        self.wait_lever_expired("retry_cap", 10)
        after = self.phase(30, "cap released")
        check("cpu: retry cap does not permanently heal", not self.healthy(self.tail(after, 15)),
              f"during cap: {fmt(self.tail(cap, 10))} | after: {fmt(self.tail(after, 15))}")
        self.lever("db/failover", {"ttl_s": 60})
        fo = self.phase(30, "db failover")
        check("cpu: db failover does not heal", not self.healthy(self.tail(fo, 15)), fmt(self.tail(fo, 15)))
        self.reset()

    def scenario_levers(self) -> None:
        print("\n=== control API levers")
        self.reset()
        c = self.ctl
        check("levers: bad ttl -> 400", c.post("/admin/retry_override", json={"max_retries": 0, "ttl_s": 0})
              .status_code == 400)
        check("levers: ttl above max -> 400", c.post("/admin/retry_override",
                                                      json={"max_retries": 0, "ttl_s": 301}).status_code == 400)
        check("levers: bad params -> 400", c.post("/admin/shed", json={"fraction": 2, "ttl_s": 5})
              .status_code == 400)
        check("levers: DELETE idempotent", c.delete("/admin/shed").status_code == 200 and
              c.delete("/admin/shed").json()["active"] is False)
        r = c.post("/admin/retry_override", json={"max_retries": 1, "ttl_s": 4}).json()
        check("levers: apply shape", {"lever_id", "params", "applied_at", "expires_at", "active"} <= set(r)
              and r["active"] and r["lever_id"] == "retry_cap", str(r))
        self.s.tick()
        time.sleep(1)
        check("levers: retry cap reaches Orders", self.s.tick()["max_retries"] == 1)
        time.sleep(4.5)
        check("levers: retry cap ttl auto-reverts", self.s.tick()["max_retries"] == 3 and
              not c.get("/admin/levers").json()["retry_cap"]["active"])
        r = c.post("/admin/db/failover", json={"ttl_s": 4})
        time.sleep(1)
        check("levers: failover reaches Payments", r.status_code == 200 and self.s.tick()["db_target"] == "standby")
        time.sleep(4.5)
        check("levers: failover ttl auto-reverts", self.s.tick()["db_target"] == "primary")
        # shed at the gateway: measure the client-visible 503 share directly
        r = c.post("/admin/shed", json={"fraction": 0.5, "ttl_s": 8})
        time.sleep(1.5)
        with httpx.Client(timeout=5) as g:
            codes = [g.post(f"{GATEWAY_URL}/checkout").status_code for _ in range(200)]
        frac = codes.count(503) / len(codes)
        check("levers: shed 50% rejects ~half at the gateway", r.status_code == 200 and 0.35 <= frac <= 0.65,
              f"503 share={frac:.2f}")
        time.sleep(8)
        with httpx.Client(timeout=5) as g:
            codes = [g.post(f"{GATEWAY_URL}/checkout").status_code for _ in range(100)]
        check("levers: shed ttl auto-reverts", codes.count(503) == 0, f"503s after expiry={codes.count(503)}")
        r = c.post("/admin/canary", json={"v2_weight": 0.05, "ttl_s": 60})
        v2_up = self._v2_running()
        check("levers: canary without orders-v2 -> 409" if not v2_up else "levers: canary accepted",
              r.status_code == (200 if v2_up else 409), r.text[:200])
        c.delete("/admin/canary")
        check("levers: GET /admin/levers lists all", set(c.get("/admin/levers").json()) ==
              {"retry_cap", "shed", "db_failover", "canary_weight"})

    def _v2_running(self) -> bool:
        try:
            return httpx.get("http://127.0.0.1:8104/healthz", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    def scenario_reset(self) -> None:
        print("\n=== reset from a live storm with levers applied")
        self.reset()
        self.fc.storm(StormFault(delay_ms=800, duration_s=10))
        self.phase(40, "storm")
        self.lever("shed", {"fraction": 0.1, "ttl_s": 300})
        self.lever("db/failover", {"ttl_s": 300})
        self.reset()
        levers = self.ctl.get("/admin/levers").json()
        check("reset: all levers inactive", not any(v["active"] for v in levers.values()), str(levers))
        check("reset: fault state none", self.fc.state().world == World.none)
        span = self.phase(30, "after reset")
        ws = self.windows(span)
        bad = [w for w in ws if not self.healthy(w)]
        check("reset: stays at healthy baseline", not bad, f"{len(ws) - len(bad)}/{len(ws)} healthy")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=["baseline", "storm", "degraded", "cpu", "levers", "reset", "repeat", "all"])
    ap.add_argument("-n", type=int, default=5, help="repeat count")
    ap.add_argument("--seconds", type=int, default=120, help="baseline duration")
    args = ap.parse_args()
    run = Run()
    plan: dict[str, Callable[[], Any]] = {
        "baseline": lambda: run.scenario_baseline(args.seconds),
        "storm": run.scenario_storm,
        "degraded": run.scenario_degraded,
        "cpu": run.scenario_cpu,
        "levers": run.scenario_levers,
        "reset": run.scenario_reset,
    }
    if args.scenario == "repeat":
        ok = sum(bool(run.scenario_storm(tag=f" #{i + 1}")) for i in range(args.n))
        check(f"repeated storm runs {ok}/{args.n}", ok == args.n)
    elif args.scenario == "all":
        for name in ("baseline", "levers", "storm", "degraded", "reset", "cpu"):
            plan[name]()
    else:
        plan[args.scenario]()
    run.reset()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for name, _, detail in failed:
        print(f"  FAILED {name}  {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
