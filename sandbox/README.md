# sandbox/ — Faultline target system (Owner 1)

A real Docker Compose system whose incidents come from queueing physics, not scripted metrics.

```
loadgen ──► Envoy :8080 ──► orders (v1 | v2 canary) ──► Envoy :8081 ──► payments ──► db-primary
 (open-loop            shed, canary split     retries live            pool of 4    └─► db-standby (failover)
  Poisson)                                    in Orders code
control :9901  – operator levers (Faultline's only write path)
faultctl :9900 – hidden fault controller (benchmark/demo only)
lab :9910      – clone manager (C6): the same stack as disposable clones `faultline-clone-<1..3>`, no faultctl
```

## Quick start

```bash
cd sandbox
docker compose up -d --build           # ~1 min first time
uv sync                                 # host tooling (validation, diagnostics)
uv run python scripts/diag.py --hidden  # live 1 s diagnostics (--hidden adds fault state; debug only)
uv run python scripts/validate.py all   # every scripted check (~12 min)
uv run uvicorn services.lab.app:app --port 9910   # clone lab (C6) manager, separate terminal
uv run python scripts/validate_lab.py all         # clone fairness, API, reproductions (~10 min)
```

## The capacity model

| Symbol | Meaning | Default | Where |
|---|---|---|---|
| R | logical checkouts/s (open loop) | 80 | `LOAD_RPS`, loadgen |
| A | attempts per logical request, 1 … 1+max_retries | 1 healthy, ~4 storm | Orders |
| T | Orders → Payments per-attempt timeout | 500 ms | `ORDERS_ATTEMPT_TIMEOUT_MS` |
| N | Payments → primary DB connection pool | 4 | `DB_POOL_SIZE` |
| S | DB service time per query | 38 ms (+~1.5 ms overhead) | `DB_BASE_MS`, `io_profile` table |
| μ | DB capacity = N / S | ≈ 100 q/s | |
| W | max wait for a pool connection | 1000 ms | `DB_ACQUIRE_TIMEOUT_MS` |
| μ_standby | failover capacity = 16 / S | ≈ 400 q/s | `STANDBY_POOL_SIZE` |

Every query holds one of N connections for exactly S (a `pg_sleep` inside `process_payment()`, whose cost
is read from the DB's own `io_profile` row), so capacity is exactly N/S and pool busy ratio ≈ λ/μ.
Queries beyond μ wait FIFO for a connection, up to W.

**The invariant that makes the storm metastable:** when Orders gives up on an attempt, the Payments work is
*not* cancelled. The query runs in its own task under `asyncio.shield`, keeps its place in the pool queue,
and occupies the DB. `late` in the diagnostics (requests that finish after the caller has hung up) shows
it; `validate.py storm` checks it.

* **Healthy:** λ = R ≈ 80 < μ ≈ 100. Waits are ~50 ms against T = 500 ms; retry_ratio 1.00.
* **World A (storm):** `POST :9900/fault/storm {delay_ms: 800, duration_s: 20}` adds 800 ms per query on
  the primary for 20 s, then removes it completely. During the trigger, μ collapses to ~5 q/s, the queue
  fills, attempts time out, and Orders retries immediately. Afterwards λ = R × 4 ≈ 320 > μ ≈ 100. Every
  served query has waited ~W > T, so every attempt still times out. Nothing is injected any more; the
  retry loop sustains itself. Capping retries brings λ back to 80 < μ, the backlog (≈ μ·W) drains in ~3 s,
  and restoring retries leaves the system healthy.
* **World B (degraded DB):** `POST :9900/fault/degrade_db {capacity_qps: 40}` raises the primary's
  per-query cost to N/40 = 100 ms (persistent, invisible to the app). Now R = 80 > μ = 40. The retry cap
  removes amplification but not the overload, so the incident is still there and returns at 4× when the
  cap expires. Failover moves new queries to the standby (μ ≈ 400 > 320), which heals it.
  The standby must be bigger than R·(1+max_retries); a standby with capacity ~100 would stay stuck in the
  storm.
* **None-of-the-above (CPU starvation):** `POST :9900/fault/cpu_starve {service: payments, cpus: 0.1}` runs
  `docker update --cpus`. Each payment does ~1 ms of CPU-time work (`PAYMENTS_CPU_WORK_MS`), so 0.1 CPU
  serves roughly 50/s < R. Neither the retry cap nor DB failover heals it.

Numbers are defaults, not assumptions: each is an env var in `docker-compose.yml`. Keep R < μ < R·(1+retries)
< μ_standby and W > T.

**Margin:** a full stall of the DB/Payments path of about 0.5 s (at R=80, T=500 ms) can tip the healthy
system into a storm. A larger T or a smaller R widens that margin (T = 800 ms → ~1 s). The 5-minute baseline
check watches for spontaneous storms.

## Validated behavior (`scripts/validate.py`, Docker Desktop, macOS)

| Check | Result |
|---|---|
| healthy baseline stable (5 min, every 5 s window) | 60/60 windows healthy, retry 1.00, success 100%, DB p99 ~190 ms |
| storm persists ≥ 60 s after the trigger ends | 12/12 windows at retry 4.0×, success 0%, DB still completing ~101 q/s of abandoned work |
| retry cap (0, 20 s) heals the storm | healthy ~3 s after the cap |
| stays healed ≥ 60 s after the cap expires | 12/12 windows healthy, retries back to 3 |
| degraded DB: cap removes amplification but doesn't heal | retry 1.0×, success 0% |
| degraded DB: incident returns after the cap | retry 4.0×, success 0% |
| degraded DB: failover heals | success 100%, DB p99 50 ms |
| CPU starvation: neither cap nor failover heals | 7/7 |
| reset from a live storm with levers applied | healthy in 7–16 s, all levers off, stays healthy |
| repeated storm runs (reset → storm → cap → release, back to back) | 5/5 (42/42 checks); every run retry 3.96–3.98× → 1.00×, reset 7–9 s |
| lever API: 400s, 409 canary without v2, TTL auto-revert, shed 50% → 50% 503s | 15/15 |

Logs from each run go to `runs/` (gitignored): `uv run python scripts/validate.py <scenario> > runs/x.log`.
Owners 2/3/4: see **[INTEGRATION.md](INTEGRATION.md)** for ports, API usage and telemetry mapping.

## APIs

### Control service `:9901` (Faultline may use this)

As specified in `contracts/README.md` (C3): `POST`/`DELETE /admin/{retry_override,shed,db/failover,canary}`,
`GET /admin/levers`, `GET /healthz`. Every POST requires `ttl_s` (bounded by the lever's `max_ttl_s`).

Dead-man switch: Orders and Payments store the expiry themselves, so a retry cap or failover reverts even
if both Faultline and the control service crash. Envoy runtime (shed, canary) has no TTL. The control
service reverts it on expiry, re-asserts it every second, and zeroes it when it starts.

### Fault controller `:9900` (hidden, C5)

`POST /fault/{storm,degrade_db,cpu_starve,reset}`, `GET /fault/state`, all returning `FaultState`. Extras for
bench: `POST /debug/reset` (reset and return the verified baseline window), `POST /debug/load {"rps": 60}`
(change R; reset restores the default), `GET /debug/config`.

**`reset` blocks until the system is healthy** (typically 7–16 s, bounded by `RESET_TIMEOUT_S`), so give
the client a longer timeout than its 5 s default: `HttpFaultController(timeout_s=150)`. What reset does:

1. Clears the DB cost on both DBs and restores CPU limits.
2. Reverts every lever through the control API and restores the default load.
3. Drains: sets Orders retries to 0 so λ < μ and the queue and any storm empty out.
4. Releases retries.
5. Requires a healthy 5 s window before returning 200, retrying up to 3 rounds, otherwise 503.

## Telemetry notes for Owner 2

`GET /stats` on orders (:8101), payments (:8102) and loadgen (:8103) returns cumulative counters, gauges and
latency histograms. `services/common/probe.py` turns two snapshots into rates and quantiles.

OTel auto-instrumentation is wired for `payments`, `orders`, `orders-v2` and `loadgen` via env +
`opentelemetry-instrument` (no code changes; `control`/`faultctl` stay uninstrumented). All four ship
OTLP http/protobuf to the compose project's `otel-collector` (`otel/collector.yaml`), which also scrapes
Envoy `:9902/stats/prometheus`, tags every signal `deployment.environment=production|clone-<slot>`, and
drops `/internal/*` `/admin/*` `/stats` `/healthz` `/rate` spans, `*fault*` metric names and all
`db.statement`/`db.query.text` attributes before exporting. Sink is the local `debug` exporter unless
`FAULTLINE_ELASTICSEARCH_URL` is set in `.env` (exports to `<url>/_otlp`, Elasticsearch 9.x native
OTLP intake, authenticated with `FAULTLINE_ELASTICSEARCH_API_KEY`);
`OTEL_SDK_DISABLED=true` turns instrumentation off entirely. Compose reads `sandbox/.env` only —
`ln -sf ../.env sandbox/.env` to share the root file. The running production containers keep the old
image; this activates when the project is next recreated.

Clone collectors also tee OTLP JSON batches to `/tmp/otel/records.jsonl` (`FAULTLINE_OTEL_TEE=1`, set
by the lab manager via `otel/sink-tee.yaml` / `sink-elastic-tee.yaml`); `validate_lab.py fairness`
reads those records rather than container logs, and optionally cross-checks `traces-*` in Elasticsearch
when `FAULTLINE_ELASTICSEARCH_URL` + `FAULTLINE_ELASTICSEARCH_API_KEY` are set (SKIPped otherwise).

After changing OTel deps / `requirements.txt`, rebuild the shared app image or clones fail with
`opentelemetry-instrument: not found` (announce production recreates in chat first per the
shared-stack rule):

```
cd sandbox && docker compose build --quiet payments && docker compose up -d --force-recreate
```

Keep these choices in any OTel-derived telemetry too, because they are what keeps World A and World B
ambiguous:

* **DB query latency = client-side time from issuing the query to the result, including pool wait**
  (`db_query` histogram). Don't emit execution-only time (asyncpg auto-instrumentation spans or
  `pg_stat_statements`). Execution is 38 ms in a storm and 100 ms when degraded, which gives the world away.
* **`db.qps` = queries issued** (`db_queries_issued`, ≈ 320/s in both worlds). *Completed* throughput
  differs by physics (≈ μ = 100 in a storm, ≈ 40 degraded). That's a real, honest signal, but it makes
  passive diagnosis easier if it's reported.
* Log lines are rate-limited to 1 per second per template, with a `(+N similar)` suffix for counts.
* Envoy's shed mechanism is its HTTP fault filter, so Envoy stats contain `gateway.fault.*`. Don't ingest
  Envoy fault-filter stats (they're lever state, but the word trips the fairness check).
* No hidden state reaches the app: the DB cost lives in `io_profile`, which the app never reads, and
  fault-controller state is only on :9900.

## Clone lab (C6, `services/lab`)

`uv run uvicorn services.lab.app:app --port 9910` on the host. A clone is `docker-compose.yml` +
`clone.override.yml` under project `faultline-clone-<slot>` with `PORT_*` shifted by 1000·slot, started without
`faultctl`, configured only from the `CloneSpec` (retry policy, workload rate, optional `patch_ref` for
orders-v2). Lab actions move the same physical knobs the fault controller moves in production (`io_profile`
cost via `psql`, `docker update --cpus`, `compose stop/start`), each with a TTL the manager enforces. The
manager refuses to touch any compose project it didn't create.

Validated (`scripts/validate_lab.py`): clone ready in ~9 s; no faultctl, clean `io_profile`, empty DB; production
`:9900` refuses clone traffic (403: faultctl accepts only its own network + host, and clone networks don't
masquerade); `db_latency 800/20 s` → self-sustaining storm that the clone's retry cap heals permanently;
`db_capacity 40` → degraded DB that only failover heals; `cpu_limit payments 0.1` → neither heals; API
validation, TTL revert, undo, reset (8–15 s), capacity 3, destroy idempotent, production levers untouched.
~300 MB / ~0.2 CPU per healthy clone. Recipes and endpoint details in [INTEGRATION.md](INTEGRATION.md).

## Canary (orders-v2)

`orders-v2` is under the `canary` profile and built from `ORDERS_V2_CONTEXT` (a repo root with the patched
`sandbox/services/orders/app.py`):

```bash
ORDERS_V2_CONTEXT=/path/to/patched/checkout docker compose --profile canary up -d --build orders-v2
curl -XPOST localhost:9901/admin/canary -d '{"v2_weight": 0.05, "ttl_s": 1800}' -H 'content-type: application/json'
```

`/admin/canary` returns 409 while orders-v2 isn't running. orders-v2 reports `version: v2` in its `/stats`
gauges (host port 8104). The retry-cap lever and reset's drain apply to it too.

## Layout

```
docker-compose.yml   envoy/envoy.yaml   postgres/init.sql   Dockerfile (one image, build context = repo root)
services/orders      retry loop + runtime override          services/payments  pool, shielded DB work, failover
services/loadgen     open-loop Poisson client               services/control   :9901 levers
services/faultctl    :9900 hidden faults + reset            services/common    stats + probe (shared with scripts)
services/lab         :9910 clone manager (C6, host process) clone.override.yml clone-only network settings
otel/                collector.yaml + sink.yaml/sink-elastic.yaml (OTLP → debug or Elastic Cloud)
scripts/diag.py      live diagnostics                       scripts/validate.py scripted checks
scripts/validate_lab.py  clone lab checks (fairness, api, storm, degraded, cpu)
scripts/sweep_lab.py     benchmark cell sweep (rps × trigger), concurrent clones, LabPatchVerifier path
```
