"""Parameter sweep and consumer-path checks for the clone lab (C6), for benchmark design.

  uv run python scripts/sweep_lab.py sweep       # which (rps, trigger) cells ignite a storm / degrade the DB
  uv run python scripts/sweep_lab.py concurrent  # two clones created, exercised and reset at the same time
  uv run python scripts/sweep_lab.py verify      # the LabPatchVerifier sequence: patch_ref clone, canary 1.0, recipe
  uv run python scripts/sweep_lab.py all

Runs entirely in clones through the C6 client, the clone's /stats and its C3 control. Never touches
production or the fault controller. Results go to runs/sweep-<mode>.json; exit code 1 if a check failed.

A storm cell is *valid for World A* when the storm ignites (every 5 s window in the 20 s after the
slowdown ends is an amplified incident), the clone's retry cap heals it, and it stays healthy after
the cap is released. A degraded cell is *valid for World B* when the incident develops and the retry
cap does not heal it. Both conditions are the benchmark's ground truth for "this cell is that world".
"""

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from _common import Sampler, probe
from faultline_contracts.clone import CloneInfo, CloneSpec, CloneStatus, HttpCloneLab, WorkloadSpec

LAB_URL = "http://127.0.0.1:9910"
RUNS = Path(__file__).resolve().parents[1] / "runs"
STORM_GRID = [(rps, delay, dur) for rps in (60, 80, 100) for delay in (400, 800) for dur in (10, 20)]
DEGRADED_GRID = [(rps, cap) for rps in (60, 80) for cap in (30, 40, 60)] + [(80, 50)]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    return ok


def fmt(r: dict[str, Any] | None) -> str:
    if not r:
        return "(no data)"
    keys = ("logical_qps", "retry_ratio", "ok_ratio", "db_issued_qps", "db_p99_ms")
    return " ".join(f"{k}={r[k]:.2f}" if isinstance(r[k], float) else f"{k}={r[k]}" for k in keys)


class Clone:
    """One clone plus a 1 s sampler over its /stats."""

    def __init__(self, lab: HttpCloneLab, name: str, quiet: bool = True, **spec: Any) -> None:
        self.lab = lab
        t0 = time.monotonic()
        self.info: CloneInfo = lab.create(CloneSpec(name=name, **spec))
        self.create_s = time.monotonic() - t0
        self.timeout_ms = self.info.spec.retry_policy.timeout_ms
        out = open("/dev/null", "w") if quiet else sys.stdout
        self.s = Sampler(urls=self.info.endpoints.stats_urls, out=out)
        self.ctl = httpx.Client(base_url=self.info.endpoints.control_url, timeout=10)

    @property
    def id(self) -> str:
        return self.info.clone_id

    def phase(self, seconds: float, label: str = "") -> tuple[int, int]:
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

    def cap(self, max_retries: int | None, ttl_s: int = 15) -> None:
        if max_retries is None:
            self.ctl.delete("/admin/retry_override").raise_for_status()
        else:
            self.ctl.post("/admin/retry_override", json={"max_retries": max_retries, "ttl_s": ttl_s}).raise_for_status()

    def reset(self) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            info = self.lab.reset(self.id)
            ok, detail = info.status == CloneStatus.ready, ""
        except httpx.HTTPStatusError as e:
            ok, detail = False, e.response.text[:200]
        return {"ok": ok, "seconds": round(time.monotonic() - t0, 1), "detail": detail}

    def destroy(self) -> None:
        self.lab.destroy(self.id)


def amplified_incident(w: dict[str, Any]) -> bool:
    return probe.is_incident(w) and (w["retry_ratio"] or 0) > 2.0


# ---- sweep ------------------------------------------------------------------------------------
def storm_cell(c: Clone, rps: int, delay_ms: int, dur_s: int) -> dict[str, Any]:
    c.lab.set_workload(c.id, WorkloadSpec(rps=rps))
    c.phase(10, "settle")
    c.lab.apply(c.id, "db_latency", {"extra_ms": delay_ms}, ttl_s=dur_s)
    c.phase(dur_s + 1, "db_latency")
    post = c.phase(25, "after trigger")
    ws = c.windows(post)[-4:]
    ignited = bool(ws) and all(amplified_incident(w) for w in ws)
    cell: dict[str, Any] = {"world": "storm", "rps": rps, "delay_ms": delay_ms, "duration_s": dur_s,
                            "ignited": ignited, "post": fmt(c.tail(post, 20))}
    if ignited:
        c.cap(0, ttl_s=15)
        cap = c.phase(15, "cap 0")
        cell["cap_heals"] = c.healthy(c.tail(cap, 8))
        rel = c.phase(15, "released")
        cell["stays_healed"] = c.healthy(c.tail(rel, 10))
        cell["valid_world_a"] = cell["cap_heals"] and cell["stays_healed"]
    else:
        cell["valid_world_a"] = False
    cell["reset"] = c.reset()
    return cell


def degraded_cell(c: Clone, rps: int, capacity: int) -> dict[str, Any]:
    c.lab.set_workload(c.id, WorkloadSpec(rps=rps))
    c.phase(10, "settle")
    h = c.lab.apply(c.id, "db_capacity", {"capacity_qps": capacity}, ttl_s=600)
    dev = c.phase(25, "db_capacity")
    ws = c.windows(dev)[-3:]
    incident = bool(ws) and all(probe.is_incident(w) for w in ws)
    amplified = bool(ws) and all(amplified_incident(w) for w in ws)
    cell: dict[str, Any] = {"world": "degraded", "rps": rps, "capacity_qps": capacity, "incident": incident,
                            "amplified": amplified, "developed": fmt(c.tail(dev, 15))}
    if incident:
        c.cap(0, ttl_s=15)
        cap = c.phase(15, "cap 0")
        cell["cap_heals"] = c.healthy(c.tail(cap, 8))
        cell["valid_world_b"] = not cell["cap_heals"]
    else:
        cell["valid_world_b"] = False
    c.lab.undo(h)
    cell["reset"] = c.reset()
    return cell


def print_table(cells: list[dict[str, Any]]) -> None:
    print("\nworld     rps  trigger            result                              reset")
    for x in cells:
        if x["world"] == "storm":
            trig = f"latency {x['delay_ms']}ms x {x['duration_s']}s"
            res = ("VALID A" if x["valid_world_a"] else "ignited, cap %s" % ("heals but storm returns" if x.get("cap_heals") else "does NOT heal")) \
                if x["ignited"] else "no ignition"
        else:
            trig = f"capacity {x['capacity_qps']} q/s"
            res = "VALID B" if x["valid_world_b"] else ("incident but cap heals" if x["incident"] else "no incident")
        r = x["reset"]
        print(f"{x['world']:<9} {x['rps']:>3}  {trig:<18} {res:<35} {'ok' if r['ok'] else 'FAIL'} {r['seconds']}s")


def run_sweep(lab: HttpCloneLab, storm: bool, degraded: bool) -> list[dict[str, Any]]:
    c = Clone(lab, "sweep")
    check("sweep clone ready", c.info.status == CloneStatus.ready, f"{c.create_s:.1f}s")
    cells: list[dict[str, Any]] = []
    try:
        grid = ([("storm", g) for g in STORM_GRID] if storm else []) + ([("degraded", g) for g in DEGRADED_GRID] if degraded else [])
        for i, (world, g) in enumerate(grid, 1):
            print(f"\n--- cell {i}/{len(grid)}: {world} {g}", flush=True)
            cell = storm_cell(c, *g) if world == "storm" else degraded_cell(c, *g)
            cells.append(cell)
            print(json.dumps(cell), flush=True)
            if not cell["reset"]["ok"]:
                check(f"reset after {world} {g}", False, cell["reset"]["detail"])
                c.destroy()
                c = Clone(lab, "sweep")
                check("replacement sweep clone ready", c.info.status == CloneStatus.ready, f"{c.create_s:.1f}s")
    finally:
        c.destroy()
    resets = [x["reset"] for x in cells]
    check(f"clone reset succeeded in {sum(r['ok'] for r in resets)}/{len(resets)} cells", all(r["ok"] for r in resets),
          f"{min(r['seconds'] for r in resets)}-{max(r['seconds'] for r in resets)}s" if resets else "")
    check("default storm cell (80 rps, 800 ms x 20 s) is valid World A",
          any(x["world"] == "storm" and (x["rps"], x["delay_ms"], x["duration_s"]) == (80, 800, 20) and x["valid_world_a"] for x in cells) or not storm)
    check("default degraded cell (80 rps, capacity 40) is valid World B",
          any(x["world"] == "degraded" and (x["rps"], x["capacity_qps"]) == (80, 40) and x["valid_world_b"] for x in cells) or not degraded)
    print_table(cells)
    return cells


# ---- concurrent -------------------------------------------------------------------------------
def run_concurrent(lab: HttpCloneLab) -> dict[str, Any]:
    print("\n=== concurrent: two investigators create, inject, reset and destroy at the same time")
    clones: dict[str, Clone | Exception] = {}
    out: dict[str, Any] = {}

    def make(name: str) -> None:
        try:
            clones[name] = Clone(lab, name)
        except Exception as e:  # noqa: BLE001 - recorded as a failed check
            clones[name] = e

    def exercise(name: str) -> None:
        c = clones[name]
        assert isinstance(c, Clone)
        c.phase(8, "baseline")
        base_ok = c.healthy(c.tail((0, len(c.s.hist) - 1), 6))
        if name == "inv-a":
            c.lab.apply(c.id, "db_latency", {"extra_ms": 800}, ttl_s=20)
            c.phase(21, "trigger")
            span = c.phase(20, "post")
            repro = all(amplified_incident(w) for w in c.windows(span)[-3:])
        else:
            h = c.lab.apply(c.id, "db_capacity", {"capacity_qps": 40}, ttl_s=600)
            span = c.phase(25, "degraded")
            repro = all(probe.is_incident(w) for w in c.windows(span)[-3:])
            c.lab.undo(h)
        out[name] = {"create_s": round(c.create_s, 1), "baseline_healthy": base_ok, "reproduced": repro, "reset": c.reset()}

    t0 = time.monotonic()
    threads = [threading.Thread(target=make, args=(n,)) for n in ("inv-a", "inv-b")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    both = all(isinstance(c, Clone) and c.info.status == CloneStatus.ready for c in clones.values())
    check("two concurrent creates both ready", both,
          f"wall {time.monotonic() - t0:.1f}s; " + "; ".join(f"{n}: {c.create_s:.1f}s" if isinstance(c, Clone) else f"{n}: {c}" for n, c in clones.items()))
    if not both:
        for c in clones.values():
            if isinstance(c, Clone):
                c.destroy()
        return {"clones": {n: str(c) for n, c in clones.items()}}
    slots = {c.info.endpoints.control_url for c in clones.values() if isinstance(c, Clone)}
    check("clones got distinct slots", len(slots) == 2, ", ".join(sorted(slots)))
    threads = [threading.Thread(target=exercise, args=(n,)) for n in clones]
    [t.start() for t in threads]
    [t.join() for t in threads]
    for n, r in out.items():
        check(f"{n} healthy baseline while the other clone starts/injects", r["baseline_healthy"])
        check(f"{n} reproduces its world while the other runs", r["reproduced"])
        check(f"{n} concurrent reset ok", r["reset"]["ok"], f"{r['reset']['seconds']}s {r['reset']['detail']}")
    threads = [threading.Thread(target=clones[n].destroy) for n in clones]  # type: ignore[union-attr]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check("both destroyed", all(i.status == CloneStatus.destroyed for i in lab.list() if i.clone_id in clones))
    return out


# ---- verify (LabPatchVerifier sequence) -------------------------------------------------------
def run_verify(lab: HttpCloneLab, patch_ref: str) -> dict[str, Any]:
    """Same steps as product/adapters/clone.py LabPatchVerifier, on the unpatched tree: the storm must
    reproduce on orders-v2 with all traffic routed there, and destroy() without reset() must be clean."""
    print(f"\n=== verify: patch_ref clone, canary_weight 1.0, db_latency 800/20 (patch_ref={patch_ref})")
    c = Clone(lab, "verify", patch_ref=patch_ref)
    check("patch_ref clone ready", c.info.status == CloneStatus.ready, f"{c.create_s:.1f}s")
    v2 = c.info.endpoints.stats_urls.get("orders-v2")
    check("stats_urls has orders-v2", v2 is not None, str(v2))
    out: dict[str, Any] = {"create_s": round(c.create_s, 1)}
    try:
        r = c.ctl.post("/admin/canary", json={"v2_weight": 1.0, "ttl_s": 300})
        check("clone canary_weight 1.0 accepted", r.status_code == 200, r.text[:120])
        http = httpx.Client(timeout=5)
        v2a = http.get(f"{v2}/stats").json()["counters"].get("requests", 0)
        c.phase(10, "v2 healthy")
        v2b = http.get(f"{v2}/stats").json()["counters"].get("requests", 0)
        v1 = c.s.window(10)["logical_qps"]
        check("orders-v2 serves all traffic", (v2b - v2a) / 10 > 40 and v1 < 5, f"v2 {(v2b - v2a) / 10:.0f}/s, v1 {v1:.1f}/s")
        # The shared stats sampler reads v1; for the replay read v2 + payments + loadgen.
        c.s.urls = {**c.info.endpoints.stats_urls, "orders": v2}
        c.lab.apply(c.id, "db_latency", {"extra_ms": 800}, ttl_s=20)
        c.phase(21, "trigger")
        span = c.phase(30, "post")
        ws = c.windows(span)[-4:]
        repro = all(amplified_incident(w) for w in ws)
        check("storm reproduces on unpatched orders-v2 (verifier would report 'failed')", repro, fmt(c.tail(span, 20)))
        out.update({"v2_rps": round((v2b - v2a) / 10), "reproduced": repro})
    finally:
        t0 = time.monotonic()
        c.destroy()
        out["destroy_s"] = round(time.monotonic() - t0, 1)
    import subprocess
    left = subprocess.run(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=faultline-clone-1"],
                          capture_output=True, text=True).stdout.split()
    check("destroy without reset leaves no containers", not left, f"{len(left)} left, {out['destroy_s']}s")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["sweep", "storm", "degraded", "concurrent", "verify", "all"])
    ap.add_argument("--patch-ref", default=str(Path(__file__).resolve().parents[2]))
    a = ap.parse_args()
    lab = HttpCloneLab(LAB_URL, timeout_s=400)
    lab.catalog()
    RUNS.mkdir(exist_ok=True)
    out: dict[str, Any] = {}
    if a.mode in ("sweep", "storm", "degraded", "all"):
        out["sweep"] = run_sweep(lab, a.mode in ("sweep", "storm", "all"), a.mode in ("sweep", "degraded", "all"))
    if a.mode in ("concurrent", "all"):
        out["concurrent"] = run_concurrent(lab)
    if a.mode in ("verify", "all"):
        out["verify"] = run_verify(lab, a.patch_ref)
    out["checks"] = [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]
    path = RUNS / f"sweep-{a.mode}.json"
    path.write_text(json.dumps(out, indent=1))
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; results in {path}")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
