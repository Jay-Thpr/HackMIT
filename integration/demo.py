"""Demo driver: one command from healthy system to incident report, no keyboard in between.

  uv run python demo.py storm                 # World A: self-sustaining retry storm  -> H_meta, cap is the fix
  uv run python demo.py degraded              # World B: degraded DB capacity          -> H_db, failover confirms
  uv run python demo.py storm --no-devin      # use the prebuilt fallback patch (Devin slow / no credits)
  uv run python demo.py storm --baseline-s 60 # shorter healthy baseline for rehearsals

What it does, in order:
  1. preflight: sandbox control (:9901), fault controller (:9900), clone lab (:9910, optional),
     credentials from --env-file (OpenAI, Devin, Elastic; each optional, each turns a real component on)
  2. C5 reset -> verified healthy
  3. start `faultline watch` with everything available switched on; stream its output here
  4. let it accrue a healthy baseline, then inject the hidden world through C5
  5. wait for Faultline to finish; print `faultline report`
  6. leave the system as Faultline left it (mitigation in place) unless --reset-after

This is benchmark/demo tooling: it is the only thing here that touches C5. Faultline itself sees
only C1 (/stats), C3 (:9901) and C6 (:9910).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from faultline_contracts.fault import DegradeDbFault, HttpFaultController, StormFault

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = ROOT / "product"
RUNS = Path(__file__).parent / "runs"
CONTROL_URL = os.environ.get("CONTROL_URL", "http://127.0.0.1:9901")
FAULT_URL = os.environ.get("FAULT_URL", "http://127.0.0.1:9900")
LAB_URL = os.environ.get("LAB_URL", "http://127.0.0.1:9910")
DEFAULT_ENV_FILE = Path("~/.config/faultline/env").expanduser()

WORLDS = {
    "storm": dict(
        inject=lambda fc: fc.storm(StormFault(delay_ms=800, duration_s=20)),
        story="20 s DB hiccup; the retries outlast it (traffic jam after the accident)",
    ),
    "degraded": dict(
        inject=lambda fc: fc.degrade_db(DegradeDbFault(capacity_qps=40)),
        story="runaway batch job halves DB capacity; the app is genuinely starved",
    ),
}


def say(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S}  {msg}", flush=True)


def load_env_file(path: Path) -> dict[str, str]:
    """Parse `export KEY=value` / `KEY=value` lines; values never leave this process."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if not m or line.lstrip().startswith("#"):
            continue
        value = m.group(2).strip().strip('"').strip("'")
        if value and not value.startswith("REPLACE_"):
            env[m.group(1)] = value
    return env


def reachable(url: str) -> bool:
    try:
        httpx.get(url, timeout=3).raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def stream(proc: subprocess.Popen, sink) -> None:
    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.rstrip("\n")
        if "developer.mozilla.org" in line:  # httpx's help-link noise
            continue
        print(f"          {line}", flush=True)
        sink.write(line + "\n")
        sink.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("world", choices=sorted(WORLDS))
    ap.add_argument("--incident", help="incident id (default: demo-<world>-<HHMMSS>)")
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="credentials file (export KEY=value lines)")
    ap.add_argument("--baseline-s", type=int, default=130, help="healthy seconds before injection (orchestrator needs ~120)")
    ap.add_argument("--no-devin", action="store_true", help="prebuilt fallback patch instead of a Devin session")
    ap.add_argument("--no-lab", action="store_true", help="skip clone investigators and clone verification")
    ap.add_argument("--no-es", action="store_true", help="do not persist to Elasticsearch")
    ap.add_argument("--max-clones", type=int, default=1)
    ap.add_argument("--timeout-s", type=int, default=1500, help="give up waiting for Faultline after this long")
    ap.add_argument("--reset-after", action="store_true", help="C5 reset once the report is out (rehearsal loops)")
    args = ap.parse_args()

    spec = WORLDS[args.world]
    incident = args.incident or f"demo-{args.world}-{datetime.now(timezone.utc):%H%M%S}"
    RUNS.mkdir(exist_ok=True)
    audit_path = RUNS / f"audit-{incident}.jsonl"
    cli_log = RUNS / f"cli-{incident}.log"

    # 1. preflight
    say(f"Faultline demo · world={args.world} · incident={incident}")
    say(f"  {spec['story']}")
    if not reachable(f"{CONTROL_URL}/healthz") or not reachable(f"{FAULT_URL}/fault/state"):
        say("sandbox not reachable: cd sandbox && docker compose up -d")
        return 2
    secrets = load_env_file(args.env_file)
    env = {**os.environ, **secrets}
    lab_on = not args.no_lab and reachable(f"{LAB_URL}/healthz")
    devin_on = not args.no_devin and bool(env.get("DEVIN_API_KEY")) and bool(env.get("DEVIN_ORG_ID"))
    openai_on = bool(env.get("OPENAI_API_KEY"))
    es_url = None if args.no_es else env.get("FAULTLINE_ELASTICSEARCH_URL")
    say("  components: "
        f"triage={'OpenAI' if openai_on else 'fixture fallback'} · "
        f"clones={'lab :9910' if lab_on else 'off'} · "
        f"patch={'Devin' if devin_on else 'prebuilt fallback'} · "
        f"elastic={'on' if es_url else 'off'}")
    if not args.no_lab and not lab_on:
        say("  (clone lab not running: cd sandbox && uv run uvicorn services.lab.app:app --port 9910)")

    # 2. reset
    fc = HttpFaultController(FAULT_URL, timeout_s=150)
    say("resetting the target to a verified healthy state …")
    fc.reset()
    say("healthy.")

    # 3. start faultline
    cmd = ["uv", "run", "faultline", "--audit-log", str(audit_path), "watch",
           "--incident", incident, "--telemetry", "sandbox", "--levers", "sandbox", "--brain", "live",
           "--detect-timeout", str(args.baseline_s + 240), "--max-clones", str(args.max_clones)]
    if lab_on:
        cmd += ["--lab-url", LAB_URL]
    if devin_on:
        cmd += ["--devin"]
    if es_url:
        cmd += ["--elasticsearch-url", es_url]
    say("starting Faultline:  faultline " + " ".join(cmd[cmd.index("watch"):]))
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(cmd, cwd=PRODUCT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    log = open(cli_log, "w")
    threading.Thread(target=stream, args=(proc, log), daemon=True).start()

    # 4. baseline, then inject
    say(f"letting Faultline watch a healthy system for {args.baseline_s}s …")
    for remaining in range(args.baseline_s, 0, -30):
        time.sleep(min(30, remaining))
        if proc.poll() is not None:
            say("Faultline exited during the baseline; see log")
            return 1
    state = spec["inject"](fc)
    say(f"*** incident injected (hidden from Faultline) — {spec['story']} ***  [{state.started_at:%H:%M:%S}]")

    # 5. wait
    t0 = time.monotonic()
    while proc.poll() is None and time.monotonic() - t0 < args.timeout_s:
        time.sleep(2)
    if proc.poll() is None:
        proc.kill()
        say(f"Faultline still running after {args.timeout_s}s; killed")
        return 1
    say(f"Faultline finished (exit {proc.returncode}) {time.monotonic() - t0:.0f}s after injection")
    report = subprocess.run(["uv", "run", "faultline", "--audit-log", str(audit_path), "report", "--incident", incident],
                            cwd=PRODUCT, env=env, capture_output=True, text=True)
    print("\n" + (report.stdout or report.stderr), flush=True)
    say(f"audit: {audit_path}   cli log: {cli_log}")

    # 6. leave as-is unless asked
    if args.reset_after:
        say("resetting the target for the next run …")
        fc.reset()
        say("healthy.")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
