# Sandbox integration handoff (Owner 1 → Owners 2, 3, 4)

Start the stack: `cd sandbox && docker compose up -d --build`. It's healthy about 15 s later; check with
`curl -s localhost:9901/healthz` or run `uv run python scripts/diag.py`. Stop with `docker compose down`.
There are no volumes, so every `up` starts from a fresh DB.

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
| `faultctl` | `faultctl:8000` | **9900** | hidden fault controller (C5) | **bench/ and the demo script only** |

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
* Envoy stats at `envoy:9902/stats/prometheus`, reachable only inside the Compose network (e.g. from an
  OTel collector container you add to this compose project).

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
| `db.pool_busy_ratio` | Δ`db_busy_s` / (Δt × gauge `pool_size`) |
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
```

## Tunables

These are env vars in `docker-compose.yml`; change them with a `.env` file next to it. The defaults are
the validated set; if you change one, re-run `validate.py all`.
`LOAD_RPS=80 ORDERS_MAX_RETRIES=3 ORDERS_ATTEMPT_TIMEOUT_MS=500 DB_POOL_SIZE=4 DB_BASE_MS=38
DB_ACQUIRE_TIMEOUT_MS=1000 STANDBY_POOL_SIZE=16 PAYMENTS_CPU_WORK_MS=1 PAYMENTS_CPUS=2 RESET_TIMEOUT_S=120`
