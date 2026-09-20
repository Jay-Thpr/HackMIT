"""C5-equivalent fault injection for the advanced distributed stack. Bench-side only.

The Docker sandbox keeps its hidden causes in the fault controller on :9900, which Faultline
may never read. The advanced stack had no equivalent: `/lab/actions` is `_clone_only()`, so
nothing could make the production namespace unhealthy, and the five qualification cases stayed
`not_run`. This is that missing half.

It runs on the host against the private kubeconfig, so the responder cannot observe it: Faultline
sees only the control service's `/snapshot` and `/catalog`. Nothing here is importable from
`faultline/` -- same boundary as `faultline_contracts.fault`.

Two causes, deliberately chosen to look alike from the outside. Both starve fulfillment of a
shard's work, so `outstanding` and `oldest_pending_ms` climb for the tenants on that shard:

  replica-lag    pause WAL replay on shard N's replica  -> resource.shard_N_replica.replication_lag_bytes
  worker-starve  throttle worker N's CPU limit          -> resource.worker_N.* and tenant backlog

Telling them apart needs an experiment, which is the point.

    uv run python integration/advanced_faults.py status
    uv run python integration/advanced_faults.py replica-lag --shard 1
    uv run python integration/advanced_faults.py worker-starve --worker 1 --millicores 30
    uv run python integration/advanced_faults.py reset
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]
KUBECONFIG = WORKTREE / ".faultline" / "advanced" / "kubeconfig"
CONTEXT = "kind-faultline-advanced"
NAMESPACE = "faultline-advanced"
SPEC = json.loads((WORKTREE / "sandbox" / "advanced" / "spec.json").read_text())
SHARDS = SPEC["postgres"]["shards"]
WORKERS = SPEC["replicas"]["worker"]
HEALTHY_CPU = SPEC["resources"]["app"]  # requests/limits to restore on reset
DB = ["-U", SPEC["postgres"]["user"], "-d", SPEC["postgres"]["database"]]


def _kubectl(args: list[str], *, timeout: int = 60) -> str:
    if not KUBECONFIG.exists():
        raise SystemExit(f"advanced cluster kubeconfig missing at {KUBECONFIG}; bring the stack up first")
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(KUBECONFIG), "--context", CONTEXT, "-n", NAMESPACE, *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode:
        raise SystemExit(f"kubectl {' '.join(args[:3])} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def _psql(shard: int, sql: str) -> str:
    return _kubectl(["exec", f"shard-{shard}-replica-0", "-c", "postgres", "--",
                     "psql", *DB, "-tAc", sql]).strip()


def _replay_paused(shard: int) -> bool:
    return _psql(shard, "select pg_is_wal_replay_paused()") == "t"


def _cpu(worker: int) -> dict:
    raw = _kubectl(["get", "deployment", f"worker-{worker}", "-o",
                    "jsonpath={.spec.template.spec.containers[0].resources}"])
    return json.loads(raw) if raw.strip() else {}


def _set_cpu(worker: int, requests: str, limits: str) -> None:
    # the container is named after its deployment; a strategic merge on the wrong name silently
    # tries to append a second, imageless container instead of editing this one
    patch = {"spec": {"template": {"spec": {"containers": [
        {"name": f"worker-{worker}",
         "resources": {"requests": {"cpu": requests}, "limits": {"cpu": limits}}}]}}}}
    _kubectl(["patch", "deployment", f"worker-{worker}", "--type", "strategic",
              "-p", json.dumps(patch)])


def _check(value: int, limit: int, label: str) -> int:
    if not 0 <= value < limit:
        raise SystemExit(f"{label} must be in [0, {limit})")
    return value


def cmd_status(args) -> int:
    paused = {f"shard_{s}_replica": _replay_paused(s) for s in range(SHARDS)}
    cpu = {f"worker_{w}": _cpu(w).get("limits", {}).get("cpu") for w in range(WORKERS)}
    healthy = not any(paused.values()) and all(v == HEALTHY_CPU["limits"]["cpu"] for v in cpu.values())
    print(json.dumps({"healthy": healthy, "replay_paused": paused, "worker_cpu_limit": cpu}, indent=2))
    return 0


def cmd_replica_lag(args) -> int:
    shard = _check(args.shard, SHARDS, "shard")
    if _replay_paused(shard):
        print(json.dumps({"shard": shard, "already": "paused"}))
        return 0
    _psql(shard, "select pg_wal_replay_pause()")
    print(json.dumps({"cause": "replica-lag", "shard": shard, "paused": _replay_paused(shard)}))
    return 0


def cmd_worker_starve(args) -> int:
    worker = _check(args.worker, WORKERS, "worker")
    if not 1 <= args.millicores <= 500:
        raise SystemExit("millicores must be in [1, 500]")
    value = f"{args.millicores}m"
    _set_cpu(worker, value, value)
    print(json.dumps({"cause": "worker-starve", "worker": worker,
                      "cpu": _cpu(worker).get("limits", {}).get("cpu")}))
    return 0


def cmd_reset(args) -> int:
    restored = {"resumed": [], "cpu_restored": []}
    for shard in range(SHARDS):
        if _replay_paused(shard):
            _psql(shard, "select pg_wal_replay_resume()")
            restored["resumed"].append(shard)
    for worker in range(WORKERS):
        if _cpu(worker).get("limits", {}).get("cpu") != HEALTHY_CPU["limits"]["cpu"]:
            _set_cpu(worker, HEALTHY_CPU["requests"]["cpu"], HEALTHY_CPU["limits"]["cpu"])
            restored["cpu_restored"].append(worker)
    still_paused = [s for s in range(SHARDS) if _replay_paused(s)]
    if still_paused:
        raise SystemExit(f"replay still paused on {still_paused}; inspect before reusing the stack")
    print(json.dumps({"reset": True, **restored}))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status").set_defaults(fn=cmd_status)
    lag = commands.add_parser("replica-lag")
    lag.add_argument("--shard", type=int, default=1)
    lag.set_defaults(fn=cmd_replica_lag)
    starve = commands.add_parser("worker-starve")
    starve.add_argument("--worker", type=int, default=1)
    starve.add_argument("--millicores", type=int, default=30)
    starve.set_defaults(fn=cmd_worker_starve)
    commands.add_parser("reset").set_defaults(fn=cmd_reset)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
