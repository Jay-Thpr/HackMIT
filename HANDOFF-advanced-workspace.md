# Handoff — one real incident on the advanced stack, rendered in the agent workspace tab

Worktree: `/Users/jt/Desktop/HackMIT/.faultline/worktrees/distributed-demos`
Read `AGENTS.md` first (its "Advanced distributed stack — LAUNCHED and verified live" section is current; the section below it is superseded and says so).

## Goal

Run **one incident** against the advanced Kubernetes stack and have it replay in the Product UI's **agent workspace tab** with the real distributed topology — Kafka partitions, Postgres shards and replicas, workers, tenants. The comparison ("Compare responders") view is explicitly *not* the deliverable.

## State: what already works

The cluster is **up and healthy** (18 pods): 3 Kafka brokers, 3 Postgres shards each with a streaming replica, 2 API replicas, relay, 3 workers, redis, loadgen, control.

```bash
cd sandbox && uv run --group advanced python -m advanced.cli status     # pods + services
```

**Host access to the in-cluster control service** (dies with the shell that starts it):

```bash
cd /Users/jt/Desktop/HackMIT/.faultline/worktrees/distributed-demos
export KUBECONFIG="$PWD/.faultline/advanced/kubeconfig"
kubectl --context kind-faultline-advanced -n faultline-advanced \
  port-forward service/control 19921:8000 &
TOKEN=$(kubectl --context kind-faultline-advanced -n faultline-advanced \
  get secret advanced-secrets -o jsonpath='{.data.control-token}' | base64 -d)
curl -sS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:19921/snapshot | head -c 400
```

`/snapshot` yields 10 services, 18 resources (6 Kafka partitions, 3 shard replicas, 6 tenants, 3 workers), 16 edges, **79 C1 metrics**.

### Built and verified this session

| File | What it is |
|---|---|
| `integration/advanced_faults.py` | The missing **C5** half: host-side fault injection, hidden from Faultline. `status` / `replica-lag` / `worker-starve` / `reset`. |
| `product/src/faultline_product/adapters/advanced.py` | `AdvancedTelemetrySource` (polls `/snapshot`, converts with Owner 2's `fingerprint_from_distributed`, asserts per-tenant SLOs) and `AdvancedLeverAdapter` (C3 over `/catalog` + `/actions`). |

Both are new, self-contained and uncommitted. `AGENTS.md` is modified. Nothing else in this worktree was touched.

### Measured — do not re-derive

Fault signatures (all fully reversible, verified by returning to baseline):

| Injected cause | Tenant backlog | Replica lag | User impact |
|---|---|---|---|
| `worker-starve --worker 1 --millicores 20` | 44 outstanding, oldest 41 s | none | **severe**, confined to that worker's tenants (b/d/e) |
| `replica-lag --shard 1` | none | 645 KB climbing | **none** |
| shard-primary CPU throttled to 20m | none | none | **none** (12 rps is far below DB capacity) |

- **Worker starvation is the only cause that currently produces a user-visible incident.** Use it.
- The three causes are **not** mutually ambiguous — disjoint signatures, so telemetry alone separates them. Real ambiguity needs the workload raised until DB capacity matters, or a second cause on the same consumer path. Do not claim ambiguity without re-measuring.
- Backlog develops 30–45 s after injection and drains ~36 s after reset.

Token cost (measured with `faultline_telemetry.tokens`): raw `/snapshot` 6,441; one C1 window 1,383; 12 windows 16,563; 24 windows 33,123. Triage takes a single fingerprint and the judge is pure math, so expect **~5–10k tokens per incident**.

## What's left

### 1. Advanced candidate `Experiment`s (design, not wiring)

`LiveBrain(candidates: list[Experiment], ...)` takes its candidates **from the caller**; none exist for this stack. They must target the *specific* affected tenant/worker, which is the "case-driver" `AGENTS.md` lists as unfinished.

Catalog (from `GET /catalog`, parses cleanly into `LeverSpec`):

| Lever | Params | Blast radius |
|---|---|---|
| `tenant_admission` | `tenant` enum, `max_rps` 1–1000 | 16.7 % |
| `consumer_backoff` | `partition` 0–5, `backoff_ms` 100–2000 | 16.7 % |
| `read_route` | `shard`, `target` | 33.3 % |
| `cache_coalescing` | `tenant`, `enabled` | 16.7 % |
| `worker_cpu` | `worker` enum, `millicores` 50–1000 | 33.3 % |

For a worker-starvation incident the discriminating probe is `worker_cpu` raising the affected worker back to 500m: if the backlog drains while held, the worker was the constraint.

### 2. CLI wiring (optional — see the faster path)

`faultline watch` has no advanced option (`grep advanced product/src/faultline_product/cli.py` is empty). Would need `--telemetry advanced`, `--levers advanced`, a control URL and a token source.

### 3–4. Run the incident, then verify the workspace tab

**Recommended fastest path: skip the CLI and drive the real `Orchestrator` from a script.** Same production code, far less plumbing. Mirror `bench/src/faultline_bench/comparison_runtime.py::run_probe` in the `comparison-lab` worktree — it already constructs an orchestrator with `devin=None, canary=None` (which correctly skips stages 6–8):

1. Start `AdvancedTelemetrySource.start()` and collect **≥ `BASELINE_S` (120 s)** of healthy snapshots — the noise model needs them.
2. Inject: `uv run python integration/advanced_faults.py worker-starve --worker 1 --millicores 20`.
3. Wait for a sustained breach: `telemetry.wait_for_breach(timeout_s=..., sustain_s=...)`. The production detector uses 60 s; a shorter sustain is fine for a demo if you say so.
4. `orchestrator.run(incident_id, now)` with `JsonlSink` for C4 audit and `JsonlFingerprintStore` for C1 windows.
5. **Always** `uv run python integration/advanced_faults.py reset` in a `finally`.
6. Render:
   ```bash
   cd product && uv run faultline ui --port 8010 \
     --extra-audit-log <audit.jsonl> --fingerprints-log <fingerprints.jsonl>
   ```
   `ui_scenario._topology` already promotes `fp.resources` to nodes and `_resource_kind` maps `kafka*` → queue and `*shard*/replica*` → datastore, so the complex graph should appear with no UI changes.

Budget roughly 6–8 minutes of wall clock per run: 120 s baseline + ~45 s for the incident to develop + detection + a couple of ~1-minute experiment cycles.

## Traps that already cost time

- **Every advanced lever reports 100 % blast radius** from the control service (`estimate_blast_radius` is a `return 100.0` placeholder). The orchestrator refuses anything over 50 %, so *no advanced experiment can run* against the raw value. `AdvancedLeverAdapter` works around this by deriving the radius from each lever's scoping-parameter cardinality. Don't "fix" it by removing that.
- **A deployment's container is named after the deployment** (`worker-1`, not `app`). A strategic-merge patch with the wrong name silently appends a second, imageless container and the patch is rejected.
- **Postgres role is `app`, not `postgres`**, database `commerce`. `app` *can* run `pg_wal_replay_pause()`.
- **`oldest_pending_ms` is absent when healthy** (correctly — missing data is never zero), so an SLO on it alone gives healthy windows no SLO row at all. The adapter also asserts on `outstanding`, which is always present.
- **No fault path exists for advanced production.** `/lab/actions` is `_clone_only()`. That is why `integration/advanced_faults.py` exists; keep it host-side and out of anything under `faultline/`.
- **Playwright's `reuseExistingServer` will silently test the wrong app.** A vite dev server from `/Users/jt/Desktop/HackMIT/product/ui` (the main checkout) sits on port 4173; run browser tests with `FAULTLINE_UI_PORT=4199` or they pass against an app with no comparison view.

## Housekeeping

- **Nothing is committed** — 3 entries here, 33 in the `comparison-lab` worktree (the comparison lab plus Pilot 1–3 recordings).
- The kind cluster runs on top of the Docker sandbox stack. Reclaim with `kind delete cluster --name faultline-advanced` (kind lives at `.faultline/bin/kind`, not on `PATH`).
- Docker Desktop memory was raised from 7.7 GiB to 23.7 GiB to clear the 12 GiB preflight. Don't let it get reset.
- **A Datadog API key was exposed in a screenshot earlier and still needs rotating.**

## Separate, already finished — don't redo

In the `comparison-lab` worktree, Pilot 3 is a completed measured result: probe arm 2/2 correct on storm and degraded-DB, read-only arm 1/2 (it answers `H_db` in both worlds), 76 % fewer failed checkouts on the storm case, 71× fewer tokens. Recording at `bench/pilot3-runs/`, served copy in `runs/comparisons/`. That work is done and is not part of this handoff.
