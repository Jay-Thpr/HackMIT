# Handoff to the Owner 1 (C6 clone lab) agent — from the integration harness

Context: `integration/` holds two bench-side harnesses that run against the live `faultline-sandbox` stack:

* `smoke_sandbox.py` — hero sequence through C5 + `:9901` + `/stats` only (42/42 on the frozen sandbox).
  `--cpu` adds the cpu_starve world; `--repeat N` for reliability numbers.
* `live_loop.py` — the real v5 loop: C5 inject → `faultline watch --telemetry sandbox --levers sandbox --brain live`
  → C4 audit assertions → reset. **Storm world: 18/18, H_meta confirmed live** (after the product detector fix below).

## 1. Shared-stack collisions (3 so far, please coordinate)

Three integration runs were killed mid-flight by the production stack being recreated from another session:

| When (UTC) | What happened on `faultline-sandbox` | What it broke |
|---|---|---|
| 19:38:11 | all app containers recreated (`compose up --build`), then `faultline-clone-1` DBs appeared | smoke run, step 8 (`RemoteProtocolError`) |
| ~19:59:40 | full `compose down` + `up` (DBs at "1 second", app containers in `Created`) | live-loop degraded run, orphaned a `faultline watch` |
| 20:00:40–20:01:00 | `validate.py levers` signature on production control (retry_cap 1/4 s, db_failover 4 s, shed 0.5/8 s) | live-loop degraded run #2: pre-reset check saw `db_failover: active` (the run itself still passed: H_db confirmed) |

Requests:

* **Never `docker compose down` / `up --build` the production project without announcing it.** Anything the
  integration harness, the product loop or the benchmark has in flight dies. The 9 pm milestone runs and the
  overnight benchmark both need the production project stable for minutes at a time.
* The clone manager must drive clones with an explicit `-p faultline-clone-<k>` **and** `--project-directory`/env
  such that no code path can fall through to the default project name. The 19:59 event looked like a
  create/reset path that reached the production project. If it was a manual rebuild instead, say so and ignore this.
* The image rebuild I did at 19:37 (`docker compose up -d --build`) already picked up your `orders/app.py`
  `timeout_ms` change; the smoke test passed steps 1–7 on it before the collision.

## 2. Things the harness needs from the clone lab (no contract change)

* `CloneInfo.endpoints.stats_urls` must include `orders`, `payments`, `loadgen` — the same three the production
  `/stats` mapping uses. `smoke_sandbox.py` reads `STATS_URLS`/`CONTROL_URL`/`FAULT_URL` from env, so pointing
  it at a clone is: `ORDERS_STATS_URL=http://127.0.0.1:9101 PAYMENTS_STATS_URL=…:9102 LOADGEN_STATS_URL=…:9103
  CONTROL_URL=http://127.0.0.1:10901`. Only the C5 steps need swapping for C6 `db_latency 800/20s` and
  `db_capacity 40`; I will do that once `services/lab` is on main — no need for you to.
* `/stats` gauge `attempt_timeout_ms` is what the harness uses as the health threshold (DB p99 must be below it).
  Your change makes it reflect the override; good — keep it that way so a clone with `retry_policy.timeout_ms`
  set is judged against its own timeout.
* Please keep `reset` semantics identical to `faultctl.do_reset` (levers off, load restored, drain, healthy window
  or 503) — `live_loop.py` and the future benchmark treat 503 as "don't score", so a clone reset that returns
  200 on an unhealthy clone would silently poison results.

## 3. Findings on your side of the fence (informational, sandbox behaved correctly in every run)

* Detector timing, not physics, decided the first live run: the trigger was still active when the cap went on,
  so DB latency stayed high and the storm was diagnosed as H_db. Fixed in product (`--detect-sustain 60`).
  For the INTEGRATION.md "wait ≥ duration_s + 10 s" advice: it is now enforced by the product detector too.
* During `db_failover` payments' `pool_size` gauge switches to the standby pool (16), so `db.pool_busy_ratio` drops
  to ~0.2 at unchanged qps. Not a bug, but worth one line in INTEGRATION.md so telemetry consumers expect it.
* Bench-side `reset()` took 7.1 s / 9.7 s / 7.1 s across the smoke run (documented 7–16 s).
