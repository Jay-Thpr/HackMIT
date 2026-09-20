"""Run the chaos catalogue and print a table; write a JSON report to runs/chaos-<utc>.json.

  uv run python -m chaos                 # everything
  uv run python -m chaos -k lever -v     # a family, with Faultline's own output per run
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .cases import ALL_CASES
from .harness import run_case


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-k", default="", help="only cases whose id contains this substring")
    ap.add_argument("-v", "--verbose", action="store_true", help="print Faultline's output for every run")
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args()

    cases = [c for c in ALL_CASES if args.k in c.id]
    rows, bad = [], 0
    print(f"{'case':32} {'seed':>4} {'hidden':12} {'final':10} {'diagnosis':18} {'applies':>7}  invariants")
    for case in cases:
        for seed in case.seeds:
            o = run_case(case, seed)
            expected = case.expected(o.final, o.diagnosis)
            problem = not (o.ok and expected)
            status = "ok" if o.ok else "VIOLATED"
            if case.known_gap:
                note = "  (known gap)" if problem else "  GAP CLOSED? remove known_gap"
            else:
                note = "" if expected else "  UNEXPECTED"
            bad += problem != bool(case.known_gap)
            print(f"{case.id:32} {seed:>4} {o.hidden_world:12} {o.final:10} {str(o.diagnosis):18} {o.applies:>7}  {status}{note}")
            for f in o.invariant_failures:
                print(f"{'':32}      ! {f}")
            if args.verbose:
                for line in o.output:
                    print(f"{'':38}{line}")
                if o.raised:
                    print(f"{'':38}raised: {o.raised}")
            rows.append({**o.to_dict(), "expected": expected, "known_gap": case.known_gap, "story": case.story})

    finals = Counter(r["final"] for r in rows)
    print(f"\n{len(rows)} runs · finals {dict(finals)} · invariant violations "
          f"{sum(1 for r in rows if r['invariant_failures'])} · known gaps {sum(1 for r in rows if r['known_gap'])} · "
          f"needs attention {bad}")
    out = args.report or Path(__file__).resolve().parents[1] / "runs" / f"chaos-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"started_at": datetime.now(timezone.utc).isoformat(), "runs": rows}, indent=1, default=str))
    print(f"report: {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
