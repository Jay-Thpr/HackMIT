"""Presenter mode: a curated handful of chaos cases, real orchestrator + real math, seconds each.

  cd integration && uv run python -m chaos.demo              # paced for a live audience
  cd integration && uv run python -m chaos.demo --pace 0     # instant
  cd integration && uv run python -m chaos.demo -k lever     # one scenario

Each scenario: the story → Faultline's own stage output → the hidden truth revealed → verdict.
Everything printed is measured on this run; nothing is canned.
"""

import argparse
import sys
import time

from .cases import CASES_BY_ID
from .harness import run_case

DEMO = [
    ("storm", "Hero: transient DB hiccup, retries keep the fire burning after it is gone"),
    ("degraded", "Hero B: a batch job really did halve DB capacity — no code patch can fix hardware"),
    ("traffic_surge", "No fault at all, just traffic at 99 % of capacity: fits neither hypothesis"),
    ("telemetry_gaps", "The poller drops every 3rd window mid-incident"),
    ("lever_noop", "The control plane says 'applied' but nothing reaches the data plane"),
    ("lab_down", "The clone lab is unreachable: the production loop must carry on"),
    ("budget_exhausted", "Action budget of 2: Faultline must stop itself and page a human"),
]

HIDDEN = {"storm": "retry storm (trigger long gone)", "degraded_db": "degraded DB capacity",
          "cpu_starve": "payments CPU-starved", "none": "no fault injected"}
FINAL = {"ready": "report ready", "escalated": "escalated to a human with evidence", "paged": "stopped and paged a human",
         "refused": "refused to act", "raised": "aborted; levers expire on TTL", "no_breach": "no SLO breach, nothing to do"}


def say(text: str, pace: float, indent: int = 0) -> None:
    print(" " * indent + text, flush=True)
    time.sleep(pace)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-k", default="", help="only scenarios whose id contains this")
    ap.add_argument("--pace", type=float, default=0.35, help="seconds between printed lines")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    picks = [(cid, story) for cid, story in DEMO if args.k in cid]
    scoreboard = []
    for n, (cid, story) in enumerate(picks, 1):
        case = CASES_BY_ID[cid]
        print(f"\n{'=' * 78}\n[{n}/{len(picks)}] {cid}\n{'=' * 78}")
        say(story, args.pace)
        say("Faultline sees only /stats and the four reversible levers.", args.pace)
        print()
        t0 = time.monotonic()
        o = run_case(case, args.seed)
        wall = time.monotonic() - t0
        for line in o.output:
            say(line, args.pace, indent=4)
        if o.raised:
            say(f"[abort] {o.raised}", args.pace, indent=4)
        sim_s = (o.events[-1].ts - o.events[0].ts).total_seconds() if o.events else 0
        diagnosis = o.diagnosis
        if diagnosis is None:
            verdicts = [e for e in o.events if e.kind.value == "verdict"]
            diagnosis = f"{verdicts[-1].payload['diagnosis']} (before abort)" if verdicts else "none"
        print()
        say(f"hidden truth : {HIDDEN.get(o.hidden_world, o.hidden_world)}", args.pace, 2)
        say(f"diagnosis    : {diagnosis}  ->  {FINAL[o.final]}", args.pace, 2)
        say(f"actions      : {o.applies} applied, {len(o.active_at_end)} left on production, all TTL-bounded", args.pace, 2)
        say(f"time         : {sim_s:.0f} s simulated incident, {wall * 1000:.0f} ms wall", args.pace, 2)
        verdict = "PASS" if o.ok and case.expected(o.final, o.diagnosis) else "FAIL"
        say(f"safety       : {'8/8 invariants held' if o.ok else 'VIOLATED ' + '; '.join(o.invariant_failures)}", args.pace, 2)
        say(f"==> {verdict}", args.pace, 2)
        scoreboard.append((cid, diagnosis, o.final, verdict))

    print(f"\n{'=' * 78}\nscoreboard\n{'=' * 78}")
    for cid, diag, final, verdict in scoreboard:
        print(f"  {verdict}  {cid:20} {str(diag):20} {FINAL[final]}")
    passed = sum(v == "PASS" for *_, v in scoreboard)
    print(f"\n{passed}/{len(scoreboard)} scenarios · every lever released or TTL-bounded · a human owns every non-success")
    return 0 if passed == len(scoreboard) else 1


if __name__ == "__main__":
    sys.exit(main())
