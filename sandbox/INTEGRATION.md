# Sandbox integration handoff (Owner 1 → Owners 2, 3, 4)

Start the stack: `cd sandbox && docker compose up -d --build`. It's healthy about 15 s later; check with
`curl -s localhost:9901/healthz` or run `uv run python scripts/diag.py`. Stop with `docker compose down`.
There are no volumes, so every `up` starts from a fresh DB.

**The production project is shared.** `docker compose down` or `up --build` on `faultline-sandbox` kills every
smoke, live-loop and benchmark run in flight; announce it first. Clones are the place for anything disruptive.

Clone lab (C6, needed by investigators and the orchestrator): `uv run uvicorn services.lab.app:app --port 9910`
in a second terminal (host process; it drives `docker compose`). See [Clone lab](#owner-34-clone-lab-c6-on-9910).

## Services and ports

Host ports are bound to `127.0.0.1`. The Compose project is `faultline-sandbox`; containers are named
`faultline-sandbox-<service>-1`.

| Compose service | In-network address | Host port | Role | Who may use it |
|---|---|---|---|---|
| `envoy` | `envoy:8080` gateway, `envoy:8081` orders→payments, `envoy:9902` admin | 8080 (gateway only) | front door, shed, canary split | loadgen, demo traffic |
| `orders` | `orders:8000` | 8101 | `POST /checkout`, retry loop, `GET /stats` | telemetry reads `/stats` |
| `orders-v2` (profile `canary`) | `orders-v2:8000` | 8104 | canary build of Orders | telemetry reads `/stats` |
| `payments` | `payments:8000` | 8102 | `POST /pay`, DB pool, `GET /stats` | telemetry reads `/stats` |
| `db-primary`, `db-standby` | `:5432`, db `shop`, user `app` | none | Postgres 16 | nobody outside the sandbox |
| `loadgen` | `loadgen:8000` | 8103 | open-loop client, `GET /stats` | telemetry reads `/stats`; bench changes the rate via :9900 |
| `control` | `control:8000` | **9901** | operator levers (C3 targets) | **Faultline (Owner 4 adapter)** |
| `faultctl` | `faultctl:8000` | **9900** | hidden fault controller (C5); answers only its own network and the host | **bench/ and the demo script only** |
| lab manager (host process) | — | **9910** | clone lab (C6) | investigators (Owner 3), orchestrator (Owner 4) |

`/internal/*` endpoints on orders and payments require the `X-Sandbox-Token` header. Only `control` and
`faultctl` send it, and Faultline must not. Envoy admin (:9902) is deliberately not published on the host,
because it would let a caller bypass the lever TTLs.

## Owner 3 (bench): C5 fault controller on :9900

Use the contract client, but **raise its timeout: `reset()` blocks until the system is healthy again**
(7–16 s measured, 120 s cap).

```python
from faultline_contracts.fault import HttpFaultController, StormFault, DegradeDbFault, CpuStarveFault

fc = HttpFaultController("http://localhost:9900", timeout_s=150)
fc.reset()                                            # always first; 200 only once a healthy 5 s window is seen
fc.storm(StormFault(delay_ms=800, duration_s=20))     # FaultState(world=storm, active=True); active=False after 20 s
fc.degrade_db(DegradeDbFault(capacity_qps=40))        # persistent until reset
fc.cpu_starve(CpuStarveFault(service="payments", cpus=0.1))  # persistent until reset
fc.state()
```

* Every call returns `FaultState {world, active, params, started_at}`. A fault call replaces any earlier
  fault (it does not stack), but it leaves levers and load untouched. Call `reset()` between runs.
* `reset()` clears faults, turns off all four levers through :9901, restores the default load (80/s),
  drains queues, and returns only once the system is healthy. If it can't get there it returns **503**
  (`httpx.HTTPStatusError`). Treat that as an infrastructure failure and don't score the run.
* Timing: a storm needs ~2 s to ignite and then sustains itself, so wait ≥ `duration_s` + 10 s before
  starting detection. Degraded DB and CPU starvation are fully developed within ~5 s.
* Bench-only extras (not in C5):
  * `POST /debug/load {"rps": 60}`: change R for "varied load" runs. `reset()` restores the default.
  * `POST /debug/reset`: `reset()` plus the verified baseline window.
  * `GET /debug/config`: pool size, base cost, timeout and nominal μ.
* Bad bodies return 422 (FastAPI's validation shape). `cpu_starve` returns 501 if the Docker socket
  isn't mounted.
* Trigger sizes that work (measured): `storm` with 800 ms for 20 s. Short or weak storms may not ignite:
  the trigger must build a backlog of more than about T × μ ≈ 50 queries. `degrade_db` capacity must be
  below R to be an incident (40 works; ≥ 80 does not).

## Owner 4 (adapter): C3 control API on :9901

Exactly as in `contracts/README.md`. Measured behavior:

```bash
curl -s -XPOST localhost:9901/admin/retry_override -H 'content-type: application/json' -d '{"max_retries":0,"ttl_s":20}'
# 200 {"lever_id":"retry_cap","params":{"max_retries":0},"applied_at":"2026-…Z","expires_at":"2026-…Z","active":true}
curl -s -XDELETE localhost:9901/admin/retry_override
# 200 same shape, "active":false (idempotent)
curl -s localhost:9901/admin/levers
# {"retry_cap":{"active":false,"params":{...last params...},"expires_at":"…|null"}, "shed":…, "db_failover":…, "canary_weight":…}
```

| Lever | Path | Body | max ttl | Effect | Effective within |
|---|---|---|---|---|---|
| `retry_cap` | `/admin/retry_override` | `{"max_retries": 0-3, "ttl_s"}` | 300 | Orders max_retries (also applied to in-flight requests) | immediate |
| `shed` | `/admin/shed` | `{"fraction": 0-1, "ttl_s"}` | 300 | Envoy returns 503 for that fraction at the gateway (rounded to 1%) | < 1 s |
| `db_failover` | `/admin/db/failover` | `{"ttl_s"}` | 900 | new queries go to db-standby (μ ≈ 400) | immediate (queued queries finish on primary) |
| `canary_weight` | `/admin/canary` | `{"v2_weight": 0-1, "ttl_s"}` | 1800 | share of gateway traffic to orders-v2 (0.01% steps) | < 1 s |

* Errors are `{"detail": "..."}` with **400** (bad params or ttl out of range), **409** (canary with
  v2_weight > 0 while orders-v2 isn't running) or **502** (target unreachable).
* **status():** `active` → `active`. If `active` is false and `expires_at` is in the past, the lever
  `expired`. If you undid it, it's `undone`. After expiry `params` still shows the last applied values.
* **TTL / dead-man switch:** retry_cap and db_failover expire inside Orders and Payments themselves.
  shed and canary are reverted by `control` within about 1 s of expiry, and `control` zeroes them if it
  restarts.
* A new `POST` replaces the setting and restarts its TTL.
* **Canary:** build orders-v2 from a patched checkout with
  `ORDERS_V2_CONTEXT=<repo root> docker compose --profile canary up -d --build orders-v2`. It serves the
  same `/stats` on host port 8104 with gauge `version: "v2"`. Compare v1 (8101) against v2 (8104).
* **Budget sanity:** in a storm, `retry_cap 0` heals within ~3 s and stays healed. In World B it never
  heals, and failover heals within ~3 s.

## Owner 2 (telemetry): what the sandbox emits

**Sources:**
* `GET /stats` on orders (:8101 / `orders:8000`), orders-v2 (:8104), payments (:8102) and loadgen
  (:8103). Each returns cumulative counters, gauges, and histograms with fixed buckets (`buckets_ms`,
  where the last count is +Inf) plus `t` (unix seconds). Take the delta between two snapshots.
  `services/common/probe.py:row()` is a reference implementation of every derived number below.
* Container stdout from orders, payments, loadgen, envoy and control.
* Envoy stats at `envoy:9902/stats/prometheus`, reachable only inside the Compose network (scraped by
  the compose project's `otel-collector` every 5 s).
* OTel auto-instrumentation: `payments`, `orders`, `orders-v2` and `loadgen` run under
  `opentelemetry-instrument` (FastAPI + aiohttp-client + asyncpg) and ship OTLP http/protobuf to
  `otel-collector` (`sandbox/otel/collector.yaml`). `control` and `faultctl` are **not** instrumented,
  and `OTEL_PYTHON_EXCLUDED_URLS` plus collector `filter/fairness` keep `/internal/*`, `/admin/*`,
  `/stats`, `/healthz`, `/rate` out of the telemetry; `transform/fairness` strips `db.statement` /
  `db.query.text` (the hidden fault rides inside SQL). Spans carry `deployment.environment`
  (`production` or `clone-<slot>`).
* Sink: `FAULTLINE_ELASTICSEARCH_URL` unset → local `debug` exporter (collector container logs); set →
  OTLP http to `<url>/_otlp` (Elasticsearch 9.x native OTLP intake) with `FAULTLINE_ELASTICSEARCH_API_KEY`. Kill switch:
  `OTEL_SDK_DISABLED=true` disables instrumentation in the services. Production picks this up only
  when the production project is next recreated — the running containers still have the old image.
* Clone tee: the lab manager sets `FAULTLINE_OTEL_TEE=1`, so clone collectors also write OTLP JSON
  batches to `/tmp/otel/records.jsonl` in the collector container. `validate_lab.py fairness` reads
  those records (services, `deployment.environment`, hidden-state leak scan) and, when
  `FAULTLINE_ELASTICSEARCH_URL` + `FAULTLINE_ELASTICSEARCH_API_KEY` are set, optionally verifies the
  clone's traces landed in `traces-*` (SKIPped otherwise).
* After changing OTel deps / `requirements.txt`, rebuild the shared app image or clones fail with
  `opentelemetry-instrument: not found` (announce production recreates in chat first per the
  shared-stack rule):
  ```
  cd sandbox && docker compose build --quiet payments && docker compose up -d --force-recreate
  ```

**Must NOT ingest:** anything from `faultctl` (its logs name the faults), the `io_profile` table, Envoy
`*.fault.*` stats (shed is implemented with Envoy's fault filter, and the word trips the fairness
check), and `/internal/*`.

**Mapping to C1 keys** (per 5 s window; Δ = delta over the window):

| C1 key | Derivation |
|---|---|
| `svc.gateway.qps` / `error_rate` / `p50_ms` / `p99_ms` | Envoy `http.gateway.downstream_rq_*` and `downstream_rq_time`. The client-side equivalent is loadgen `sent`, `errors/(ok+errors)` and the `request` histogram |
| `svc.orders.qps` | Δ`requests` / Δt |
| `svc.orders.p50_ms`, `p99_ms` | `request` histogram (whole logical request, all attempts) |
| `svc.orders.error_rate` | Δ`errors` / Δ(`ok`+`errors`) |
| `svc.orders.retry_ratio` | Δ`attempts` / Δ`requests` (1.0 = no retries) |
| `svc.orders.timeout_rate` | Δ`attempt_timeouts` / Δ`attempts` |
| `svc.payments.qps`, `p50_ms`/`p99_ms`, `error_rate` | Δ`requests`; `request` histogram; Δ`errors`/Δ`requests` |
| `db.qps` | Δ`db_queries_issued` / Δt (**issued, not completed**; see below) |
| `db.query_p50_ms`, `query_p99_ms` | payments `db_query` histogram (issue → result, **includes pool wait**) |
| `db.pool_busy_ratio` | Δ`db_busy_s` / (Δt × gauge `pool_size`). During `db_failover` the gauge switches to the standby pool (16), so the ratio drops to ~0.2 at unchanged qps; expected, not a bug |
| `edge.orders.payments.*` | qps Δ`attempts`/Δt; p99 from the `attempt` histogram; error_rate Δ(`attempt_timeouts`+`attempt_errors`)/Δ`attempts` |
| `edge.payments.db.*` | qps Δ`db_queries_issued`/Δt; p99 from `db_query`; error_rate Δ`db_errors`/Δ`db_queries_issued` |
| `edge.gateway.orders.*` | Envoy `cluster.orders_v1.upstream_rq_*` (and `orders_v2` during a canary) |
| `slo.checkout.value` | `svc.gateway.p99_ms`, threshold 1000 |
| `fraud_check` | **not present in this sandbox**. Omit it (None), don't report 0 |

**Why those choices:** they keep World A and World B ambiguous using honest signals. Execution-only query
time (38 ms in a storm, 100 ms when degraded) and *completed* DB throughput (≈ 100/s vs ≈ 40/s) differ
between the worlds by physics. Don't emit those, or at least keep them out of the fingerprint. Diagnostic
extras that are fine to show on the UI: `db_queries_completed`, `db_acquire_timeouts`,
`completed_after_client_gone`, and gauges `pool_waiting`, `pool_in_use`, `in_flight`.

**Change events** (for `Fingerprint.change_events`): lever actions appear in the `control` and target
logs, e.g. `lever retry_cap applied {'max_retries': 0} for 20s`, `lever shed ttl expired, reverting`,
`retry override set: max_retries=0 for 20s`, `db target set to standby for 60s`. The audit log (C4)
is the authoritative record.

**Log templates.** Each is rate-limited to one line per second, with a `(+N similar)` suffix giving the
suppressed count:

| service | level | template |
|---|---|---|
| orders | WARNING | `payments call timed out after 500ms, retrying (attempt <n> of <m>)` |
| orders | WARNING | `payments call failed: status 503 (attempt <n> of <m>)` |
| orders | ERROR | `checkout failed: payments unavailable after <n> attempts` |
| orders | INFO | `retry override set: max_retries=<n> for <t>s` / `retry override ttl expired, max_retries back to 3` |
| payments | WARNING | `db connection pool exhausted, waited <t>ms for a connection` |
| payments | WARNING | `slow query: SELECT process_payment(...) took <t>ms` |
| payments | ERROR | `db connection error: …` / `db query failed: …` |
| payments | INFO | `db target set to standby for <t>s` / `db target ttl expired, reverting to primary` |

**Reference magnitudes** (defaults, measured, per second):

| | healthy | storm | degraded (40) | during cap (storm → heals) | during cap (degraded) |
|---|---|---|---|---|---|
| orders qps | 80 | 80 | 80 | 80 | 80 |
| retry_ratio | 1.00 | 4.0 | 4.0 | 1.0 | 1.0 |
| orders error_rate | 0 | ~1.0 | ~1.0 | 0 | ~1.0 |
| db.qps (issued) | 80 | ~320 | ~320 | 80 | 80 |
| db query p50 / p99 ms | ~50 / 100–190 | ~1150 / ~1490 | ~1150 / ~1490 | ~50 / ~140 | ~1150 / ~1490 |
| pool_busy_ratio | ~0.8 | 1.0 | 1.0 | ~0.8 | 1.0 |

## Owner 3/4 end-to-end smoke test

```bash
uv run python scripts/validate.py storm      # ~3.5 min: World A physics and levers, PASS/FAIL per check
uv run python scripts/validate.py degraded   # ~2.5 min: World B
uv run python scripts/validate_lab.py all    # ~10 min: clone lab fairness, API, and all three reproductions
```

## Owner 3/4: clone lab (C6) on :9910

Exactly `contracts/README.md` § C6; `HttpCloneLab("http://localhost:9910")`. Measured behavior:

* **A clone is the production compose file under another project** (`faultline-clone-<slot>`, slot 1–3), on its
  own network, **without `faultctl`**. It is built only from `CloneSpec`: `retry_policy` → Orders' env,
  `workload.rps` → loadgen's default rate, `patch_ref` → orders-v2 built from that checkout. `create()` returns
  `ready` after a verified healthy 5 s window: **~9 s** (≈ 11 s with `patch_ref`, more on a cold image build).
  `reset()` takes 8–15 s from any of the three incidents. `MAX_CLONES` = 3 → 4th `create` is 409.
* **Endpoints** are host ports shifted by 1000 × slot, so the production adapters work unchanged:
  slot 1 → gateway `:9080`, orders `:9101`, payments `:9102`, loadgen `:9103`, orders-v2 `:9104`, control `:10901`;
  slot 2 → `:10080`, `:10101`–`:10104`, `:11901`; slot 3 → `:11080`, `:11101`–`:11104`, `:12901`. Always read them
  from `CloneInfo.endpoints`, don't assume the scheme. `stats_urls` has `orders-v2` only when `patch_ref` is set.
* **The clone's `control_url` is a full C3 service** (retry_cap, shed, db_failover, canary_weight, same TTL rules);
  in a `patch_ref` clone `canary_weight` routes to the clone's orders-v2 (verified: weight 0.5 → v2 serves half).
* **Lab actions** (all on `POST /clones/{id}/actions`, reverted by the manager at `ttl_s`; undo is idempotent):

  | Action | What it physically does in the clone | Measured |
  |---|---|---|
  | `db_latency {extra_ms}` | every primary-DB query costs `extra_ms` more (same knob the hidden controller uses in production) | `800 / ttl 20` ignites a self-sustaining storm: after expiry 9/9 windows at retry 3.96×, db 327 q/s, p99 ~1490 ms |
  | `db_capacity {capacity_qps}` | primary capacity through the pool limited to `capacity_qps` | `40` → retry 3.99×, p99 ~1490 ms within 10 s; retry cap doesn't heal, C3 `db_failover` does (p99 50 ms) |
  | `cpu_limit {service, cpus}` | `docker update --cpus`; undo restores the original limit exactly | `payments 0.1` → incident that neither retry cap nor failover heals |
  | `retry_policy {max_retries?, timeout_ms?}` | Orders' runtime override (both fields, one TTL) | applied to in-flight requests; `/stats` gauges `max_retries`, `attempt_timeout_ms` show it |
  | `service_kill {service}` | `compose stop`, then `start` at expiry | payments answers again within ~3 s of expiry |
  | `service_restart {service}` | `compose restart`, one-shot; handle returns `expired` | |

  `db_latency` and `db_capacity` **add up** while both are active (a transient slowdown on top of a batch job).
  `retry_policy` and the clone's C3 `retry_cap` share Orders' single override slot: the last write wins.
* **Errors:** 400 bad params / ttl, 404 unknown clone or action, 409 at capacity, clone not `ready`, or
  `orders-v2` named in a clone without `patch_ref`, 503 if Docker or the clone failed (the clone is torn down and
  its `detail` says why; `create` then frees the slot).
* **Fairness, verified by `validate_lab.py fairness`:** no `faultctl` container, `io_profile` cost 38/0, empty
  payments table, production hostnames don't resolve, and production's `:9900` refuses clone traffic (403; the
  fault controller only accepts its own network and the host, and clone networks don't masquerade). The manager
  refuses to run compose/docker against any project it didn't create.
* **Cost:** ~300 MB RAM and ~20 % of one CPU per healthy clone (7–8 containers). Production + 2 clones measured
  fine on a MacBook; 3 is the hard cap.
* **Reproduction recipes** the hero hypotheses map to: H_meta → `db_latency 800, ttl 20` then wait ≥ 10 s;
  H_db → `db_capacity 40`; none-of-the-above control → `cpu_limit payments 0.1`. Whether and when to use them is
  the investigators' call.
* **Benchmark cells (measured in a clone, `scripts/sweep_lab.py sweep`; the same knobs the production fault
  controller moves, so they transfer to C5 `storm`/`degrade_db` at the same `rps`).** Every cell reset cleanly (19/19).

  | Load (rps) | World A: `db_latency` 400 or 800 ms × 10 or 20 s | World B: `db_capacity` 30 / 40 / 50 / 60 |
  |---|---|---|
  | 60 | valid: ignites, cap heals, stays healed (all 4 cells) | valid: incident at 4×, cap does not heal (30, 40, 60) |
  | 80 | valid (all 4 cells) | valid (30, 40, 50, 60) |
  | 100 | **not World A**: ignites but the cap does *not* heal (R ≈ μ, no headroom); reset 22–94 s | not measured |

  Use `rps ≤ 80` for World A; `rps = 100` looks like a storm but behaves like World B, so it belongs in the
  none-of-the-above/overload bucket if used at all. Reset after a valid cell: 7.8–8.8 s (storm), 9.8–13.8 s (degraded).
* **Consumer paths verified (`sweep_lab.py concurrent|verify`):** two clones created at once → both ready in ~9 s wall,
  distinct slots, both reproduce their world while the other runs, concurrent resets 12–13 s. The `LabPatchVerifier`
  sequence (`patch_ref` clone → clone `canary_weight 1.0` → `db_latency 800/20` → `destroy` without `reset`) works: v2
  takes 100 % of traffic, the storm reproduces on it, destroy leaves no containers.
* **Patch verification:** `CloneSpec(patch_ref=<checkout root with sandbox/services/orders/app.py patched>)`
  builds orders-v2 in the clone; then `canary_weight 1.0` via the clone's control sends it all traffic and the
  reproduction recipes above are the stress variants.
* The manager holds clone state in memory. If it restarts it removes any `faultline-clone-*` projects it finds.

## Tunables

These are env vars in `docker-compose.yml`; change them with a `.env` file next to it. The defaults are
the validated set; if you change one, re-run `validate.py all`.
`LOAD_RPS=80 ORDERS_MAX_RETRIES=3 ORDERS_ATTEMPT_TIMEOUT_MS=500 DB_POOL_SIZE=4 DB_BASE_MS=38
DB_ACQUIRE_TIMEOUT_MS=1000 STANDBY_POOL_SIZE=16 PAYMENTS_CPU_WORK_MS=1 PAYMENTS_CPUS=2 RESET_TIMEOUT_S=120`
