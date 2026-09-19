"""Run the hero experiment (cap retries 0 for 20 s, then release) against FakeWorld in each world.

    uv run python examples/run_hero.py
"""

from faultline_contracts.fakes import FakeWorld
from faultline_contracts.fault import CpuStarveFault, DegradeDbFault, StormFault

COLS = ("db.query_p50_ms", "db.qps", "svc.gateway.error_rate")


def show(world: FakeWorld, t0, label: str) -> None:
    for fp in world.series(t0, world.now, step_s=10):
        m = fp.metrics()
        t = int((fp.window_start - world.start).total_seconds())
        print(f"  {t:4d}s  {m['db.query_p50_ms']:8.0f}  {m['db.qps']:7.0f}  {m['svc.gateway.error_rate']:6.2f}   {label}")


def run(name: str, trigger) -> None:
    w = FakeWorld(seed=1)
    print(f"\n== {name} ==\n  {'t':>5}  {'db_p50':>8}  {'db_qps':>7}  {'gw_err':>6}   phase")
    t = w.start
    w.advance(60)
    show(w, t, "healthy")
    t = w.now
    trigger(w)
    w.advance(60)
    show(w, t, "fault")
    t = w.now
    h = w.apply("retry_cap", {"max_retries": 0}, ttl_s=60)
    w.advance(20)
    show(w, t, "retry_cap=0")
    t = w.now
    w.undo(h)
    w.advance(30)
    show(w, t, "released")


if __name__ == "__main__":
    run("storm (World A)", lambda w: w.storm(StormFault(duration_s=20)))
    run("degraded_db (World B)", lambda w: w.degrade_db(DegradeDbFault()))
    run("cpu_starve (none of the above)", lambda w: w.cpu_starve(CpuStarveFault()))
