"""Live diagnostics, one row per second.

  uv run python scripts/diag.py            # app telemetry only
  uv run python scripts/diag.py --hidden   # also show hidden fault state (benchmark/debug only)

Columns: gw_qps/gw_ok (client view), logic/attmp (Orders logical and attempt qps), retry
(attempts per logical request), ok (Orders success ratio), ord99 (Orders p99 ms), db_iss/db_ok
(queries issued / completed per s), db_p50/db_p99 (query latency incl. pool wait), busy (pool
busy ratio), wait (queries waiting for a connection), acqTO (pool acquire timeouts/s), late
(requests completed after the caller hung up, /s), maxr (effective max_retries), dbt (db target).
"""

import argparse
import subprocess

from _common import Sampler


def otel_preflight() -> None:
    """Production must run the OTel image: collector up, services wrapped in opentelemetry-instrument."""
    try:
        subprocess.run(["docker", "inspect", "faultline-sandbox-otel-collector-1"],
                       check=True, capture_output=True)
        subprocess.run(["docker", "exec", "faultline-sandbox-orders-1", "which", "opentelemetry-instrument"],
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("WARN  OTel stack missing or stale; rebuild + recreate production:")
        print("      cd sandbox && docker compose build --quiet payments && docker compose up -d --force-recreate")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hidden", action="store_true", help="show fault-controller state (debug only)")
    ap.add_argument("--seconds", type=float, default=1e9)
    args = ap.parse_args()
    otel_preflight()
    s = Sampler(show_hidden=args.hidden)
    try:
        s.run_for(args.seconds)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
