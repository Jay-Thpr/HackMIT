# bench — Faultline benchmark (Lane C)

Two drivers, same arms:

| Driver | World | Entry point |
|---|---|---|
| Frozen suite | `fakes.FakeWorld` (simulator) | `faultline_bench.suite.run_frozen_suite` |
| Live suite | the running sandbox (C5 `:9900`, C3 `:9901`, `/stats`) | `uv run faultline-bench-live` (`faultline_bench.live`) |

Arms: `active` (the real `faultline watch --brain live`, scored from its C4 audit log), `passive`
(LLM sees the incident fingerprint + healthy baseline, no experiment), `centroid` (nearest centroid
fitted on prior live incident windows), `llm_only` (LLM picks and interprets an experiment, no math
judge), `random` (random lever, math judge). Prompts for the LLM arms live in `llm_arms.py`.

```bash
cd bench && uv sync && uv run pytest -q                                    # 32 tests, no network
uv run faultline-bench-live --dry-run                                      # plan + time estimate, no network
uv run faultline-bench-live --worlds storm degraded --cases 2 --arms active --no-elastic   # ~13 min smoke
uv run faultline-bench-live --per-world 6 --arms active passive centroid random --no-elastic  # ~5 h
```

Reports land in `runs/bench-live-<UTC>.json` (repo root) and are rewritten after every arm-run, so an
interrupted overnight run keeps everything it measured. Per arm: `{correct, n, accuracy, unscored}`;
`n` counts scored runs only. A run is **unscored** only for infrastructure reasons: C5 reset 503,
the watch CLI exiting before injection, or a fault that never became an incident on `/stats`
("no ignition"). Everything else — including a missing verdict — is scored.

Scoring: storm → `H_meta`, degraded → `H_db`, cpu → `none_of_the_above`, no-fault → no `detect`
event. The passive/centroid/llm_only/random arms share one detection gate (≥ 10 of the last 12
windows breached) so they can also answer "no incident".

Shared-stack rule: production must be undisturbed for the whole run. No `compose down`/`up --build`,
no `integration/tests/test_smoke_sandbox.py`, no demo runs while the benchmark is going.

## Handoff — what is still to be done (as of `8a20e0d`)

Status: driver, plan, scoring, unscored rules, incremental report and unit tests are done. One live
smoke was run (`runs/bench-live-smoke.json`): 0/2, both for environmental reasons, described below.
The driver has **not yet produced a correct verdict on the live stack**.

### Blocking a meaningful run

1. **Land the Elastic write fix in product.** `faultline watch` crashed during its baseline on an
   `httpx.ReadTimeout` from the Elastic Cloud fingerprint writer (`store.py` → `elasticsearch.py`,
   re-raised out of `LiveTelemetrySource._persist`). That killed the degraded smoke case before
   injection. A fix (log a warning instead of re-raising) exists uncommitted in
   `product/src/faultline_product/adapters/live_telemetry.py` and must be on main before the freeze.
   Workaround meanwhile: `--no-elastic` (runs the CLI with `FAULTLINE_ELASTICSEARCH_URL=` empty;
   no fingerprints are persisted in that mode, so don't use it for demo evidence).
2. **Second smoke with the strong combos** — `--worlds storm degraded --cases 2 --arms active
   --no-elastic` now draws storm 1000 ms/30 s @ 100 rps and degraded capacity 30 @ 100 rps. It has
   to show `H_meta` / `H_db` scored `correct: true` before anyone trusts the numbers.
3. **Prune or pre-check the storm grid.** 600 ms/15 s @ 60 rps did not ignite (error_rate 0.09,
   retry_ratio 1.0); the driver marks such cases `unscored: no ignition`, but if many storm combos
   behave this way the storm `n` collapses. `sandbox/INTEGRATION.md` only vouches for 800 ms/20 s
   @ 80 rps. Cheapest check: a C5-only ignition sweep of the 9 storm combos (`GRID["storm"]` ×
   `RPS` in `live.py`), then drop the ones that don't sustain.

### Decide before the code freeze

4. **The ambiguity leak changes what the benchmark means.** Owner 2's check (PRD build checks)
   found `svc.payments.error_rate` (= `edge.payments.db.error_rate`) separates storm from degraded
   passively (centroid 86 % balanced, 52 % with the key hidden). If that key stays in the C1
   fingerprint, `passive` and `centroid` will not be near chance and the headline "experiments beat
   passive diagnosis" weakens. Decision pending — drop the key from C1 (Owner 2) or move World B's
   cause (Owner 1) — and it must be made before the overnight run, because all five arms see the
   frozen fingerprint shape.
5. **Runtime budget.** Full plan (40 cases × all arms) ≈ 12 h; `active` alone ≈ 4.5 h; `llm_only`
   is the most expensive extra arm. For a 3 h window: `--per-world 6 --arms active passive centroid
   random`, or `active` on everything and the other arms on a subset (`--cases`).

### Known limitations (state them, don't fix them tonight)

6. `active` reports `tokens: null` — the CLI does not put OpenAI usage into the audit log. If
   tokens-per-incident for the active arm must come from the benchmark, product needs to emit
   `usage` into an audit payload (`LiveBrain` already has a `usage_sink`). `passive`/`llm_only`
   tokens are measured.
7. The cpu world has never been exercised through the driver. `faultctl` has the Docker socket
   mounted so `cpu_starve` should work; a 501 is recorded as unscored.
8. Centroid training data is thin and phase-derived from `integration/runs/live-*.json` (≈ 60 storm
   windows, 3 degraded, no cpu class), so the centroid is wrong on cpu by construction. It is a
   documented property of the baseline; refit from `faultline-fingerprints` once the leak decision
   is made.
9. The clone arm (PRD Lane C3, v6 ablation) is not in the live driver yet; it needs Lane B's
   investigators stable on real clones first.
10. Pre-existing red test on any machine whose root `.env` holds `OPENAI_API_KEY`:
    `product/tests/test_live_brain.py::test_cli_live_brain_falls_back_without_key` (the CLI's
    dotenv loader runs after the test's `delenv` and it calls real OpenAI). Not benchmark-related.

### At the freeze (PRD task C4)

Run the agreed plan unattended, publish the JSON, and build the slide table from it and nothing
else — say which arm and how many incidents (`n`) per number.
