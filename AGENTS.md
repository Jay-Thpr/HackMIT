# AGENTS.md — Faultline

Read this first. Then `PRD.md` (product plan, v6: adds the clone lab) and `contracts/README.md` (interface spec).

## What Faultline is

An autonomous incident responder. When telemetry can't distinguish causes that fit the same symptoms, Faultline runs a safe, reversible experiment on the live system to tell them apart. Hero case: self-sustaining retry storm vs. degraded DB — cap retries; if the system stays healthy after release it was a storm, if the storm returns the DB is degraded. **The LLM proposes and explains; measurement against noise decides.**

## Agent Builder runtime branch

`brain/agent-builder-runtime` adds opt-in Agent Builder triage and clone proposals with explicit inference routing, existing schema/semantic validation and audited direct-OpenAI fallback. The math judge, action budget and C3/C6 execution remain unchanged. Default reasoning remains direct OpenAI.

This branch incorporates main through `0ce99f6`, preserving paging, resume, stale-telemetry checks and similar-incident audit data. Agent Builder errors now retain sanitized categories and HTTP status in provenance; remote bodies, keys and URLs are not logged.

- Enable proposals with `--brain live --reasoning-provider agent-builder`; add `--elastic-evidence` for scoped primary Elasticsearch context. The application retrieves bounded evidence; Agent Builder action tools, broad retrieval tools and built-in capabilities are disabled. This is not the future autonomous tool-retrieval design.
- Runtime needs `KIBANA_URL`, `ELASTIC_AGENT_BUILDER_API_KEY`, and optionally `FAULTLINE_AGENT_BUILDER_INFERENCE_ID` (default `faultline-openai-investigation`). Required deployed roles are `faultline-triage`, `faultline-clone-investigator` and, for reports, `faultline-report`. `OPENAI_API_KEY` plus the optional OpenAI SDK enables direct fallback; existing labelled fixture/seed fallback behavior remains.
- From `faultline/brain/`, `uv run python scripts/deploy_elastic_investigation_agent.py --role triage --dry-run` prints the manifest offline; `--role investigator` and `--role report` print the other tool-free manifests. The script defaults to dry-run; `--apply` is a separate operator-authorized cloud mutation, not part of tests.
- From `product/`, `uv run faultline report --incident <id> --elastic-evidence [--explain]` adds scoped evidence and optional Agent Builder-selected metrics/references. The application renders recorded values/timestamps, not model-written numerical prose or causal verdicts. Empty, rejected and truncated evidence remain explicit; the audit diagnosis remains authoritative.
- Focused checks only: Brain `uv run pytest -q tests/test_agent_builder.py tests/test_elastic_investigation.py`; Product `uv run pytest -q tests/test_agent_builder.py tests/test_elastic_evidence.py`; telemetry `uv run pytest -q tests/test_evidence.py`. Do not run the full integration suite during shared-stack activity.
- Live checks (2026-09-20): all three tool-free runtime roles were registered/verified against the explicit OpenAI `gpt-4.1` / `chat_completion` endpoint. The initial fixture triage needed one authorized retry after an unexplained `AgentBuilderError`. Subsequent proposal-only triage and investigator calls passed schema/semantic validation on frozen primary evidence from `demo-final-1`; no hypothesis was experimentally confirmed. A generated report initially passed citation membership but misstated measurements, so the report contract now selects only metrics/references and rendering uses recorded values. The revised live report passed metric/reference validation. No tool calls, fallback, clone creation or infrastructure actions ran. This is smoke coverage, not accuracy/reliability evidence.
- Evidence quality: the live read exposed negative stored rates/QPS/retry ratios. The evidence reader now rejects nonfinite/negative metrics and rates outside 0–1, retaining explicit partial/rejected status rather than clamping or inventing zeros. `retry_ratio` has no 1.0 upper bound. The origin of the stored bad counters has not been established or repaired by this work.
- Mirror verification remains blocked: no local Faultline responder was found during the check (the clone lab and sandbox were running). A running responder's actual configuration, outbox health and destination delivery must be checked when that process/location is available; root `.env` and primary-read success do not establish mirroring.

## Repository status snapshot

Status below distinguishes code merged into `origin/main` at `98e994c` (2026-09-20) from branch-only work. A feature being implemented does not prove it is deployed or live-verified. The `sandbox/bounded-clone-lifecycle` branch at `8fe32b1` contains pending PR #33; its lifetime/cleanup hardening is not yet on main. Older handoffs and historical PRD checklists may lag this snapshot.

## Repo layout

| Path | Owner | Status |
|---|---|---|
| `contracts/` | shared | Built — interfaces C1–C6, fakes, fixtures, schemas, tests |
| `sandbox/` | 1 Sandbox + storm | Built — Docker Compose target system, Envoy, fault controller :9900, levers :9901, clone lab manager :9910 (`uv run uvicorn services.lab.app:app --port 9910`, host process); see `sandbox/INTEGRATION.md` |
| `faultline/telemetry/` | 2 Telemetry + Elastic | Built on main — canonical `/stats` → C1, fingerprint/audit persistence, analytics and index templates; authoritative primary Elasticsearch plus asynchronous durable Observability display mirror; four bounded Agent Builder evidence-tool definitions and setup tooling. Raw OTel ingestion is separate from C1 conversion. Deployment and ingestion must be verified separately; see main's `sandbox/INTEGRATION.md` and `.env.example`. |
| `faultline/brain/` | 3 Brain | Built — strict OpenAI triage (`triage.py`), noise model (σ = max(std, 10% typical)), math judge with `confirms_if` gating, experiment planner (separation − blast radius), `CloneInvestigator` + agentic `InvestigatorAgent`/`AgenticCloneInvestigator` (`investigator_agent.py`: LLM proposes C6 actions with predictions under a budget, math scores reproduction/recovery/prediction at ≥75 %; live-verified on Docker clones, `live-storm-035001`), read-only Elastic Agent Builder investigation agent (`elastic_investigation.py`). **Current runtime:** triage and investigator proposals call OpenAI directly; the Elastic Agent Builder agent definition is a separate read-only explainer. **Planned (PRD v6.2):** route the Brain's LLM roles through OpenAI behind Agent Builder with closed read-only tools and direct-OpenAI fallback. This migration is not implemented; measurement still decides. See `C2_HANDOFF.md`, `C2_BRAIN_GUIDE.md`. Tests: `cd faultline/brain && uv run pytest -q` |
| `product/ (faultline_product)` | 4 Product | Built — orchestrator (8 stages + 4a investigators + 6b clone verification + Devin revise loop), C3/C1/C6 adapters, Devin API v3 adapter, git checkout of PR/branch patches, CLI; all run live incl. real OpenAI + Devin (`live-13`). Main also includes a read-only incident API (`faultline ui`) and SSE-backed live UI alongside synthetic scenarios; no browser infrastructure actions. Live command: `faultline watch --telemetry sandbox --levers sandbox --brain live --lab-url http://127.0.0.1:9910 [--devin]`; keys via `~/.config/faultline/env`. `--no-ship` = diagnose-and-mitigate (stop after mitigation + proposed fix; no build/verify/canary). `--github-pr` = `GitHubPatchAdapter` opens the prebuilt fix `product/patches/bounded-retries.patch` (touches `demo/shop/{gateway,api,cache,inventory,payments,primary-db}` + sandbox Orders) as a real PR on `Jay-Thpr/HackMIT` with the verdict's z-scores in the body; needs `GITHUB_TOKEN` (contents + pull-requests write) and `demo/shop` present on `origin/main`. With `--devin` too, Devin is primary and GitHub the fallback (`PatchFallbackChain`) |
| `bench/` | 3 Brain | Built — deterministic `FakeWorld` runner, passive-only/LLM-only/nearest-centroid/random-lever baselines, frozen simulator suite, and live-sandbox driver (`uv run faultline-bench-live`). The live clone-ablation arm and a completed frozen live benchmark remain outstanding in the checked handoff. Driver existence is not accuracy evidence. See `bench/README.md`; offline tests: `cd bench && uv run pytest -q`. |
| `integration/` | shared | Built — bench-side harnesses against the live stack: `smoke_sandbox.py` (C5 + `:9901` + `/stats` hero sequence, `--cpu`, `--repeat N`) and `live_loop.py storm|degraded` (C5 inject → real `faultline watch` → C4 audit assertions); `demo.py storm|degraded` (presenter driver; `--mode diagnose` is Demo A: ends at confirmed recovery + mitigation + PR + report, prints the measured total against the 5 min target — rehearse, do not assume). **Do not `compose down`/`up --build` production while these or the benchmark run.** Observes only; never retunes `sandbox/`. Also `chaos/`: deterministic chaos suite — real `Orchestrator` + real Brain math over `FakeWorld` (no Docker), ~38 cases × 3 seeds covering hidden-world sweeps, compound/mid-run faults, and responder-dependency failures (telemetry gaps/lag, refused/no-op/sticky levers, garbage LLM output, lab/Devin/canary down, budget); every run checks the safety invariants in `chaos/harness.py`, known gaps are `xfail(strict)`. Run: `cd integration && uv run pytest -q tests/test_chaos.py` or `uv run python -m chaos` (table + JSON in `runs/`). **`uv run pytest -q` with no path also runs `test_smoke_sandbox.py`, which injects C5 faults into production whenever the sandbox is up — never run it while a live `watch`/`demo.py` is in progress** |

Stay inside your owner's directories. Talk to other components only through `faultline_contracts`.

## Setup

```bash
cd contracts && uv sync && uv run pytest -q     # should be all green
uv run python examples/run_hero.py              # see the hero experiment in each simulated world
```

Other packages depend on contracts via `uv add --editable ../contracts` (path adjusted).

## The contracts (`contracts/src/faultline_contracts/`)

| # | Module | What | Producer → Consumer |
|---|---|---|---|
| C1 | `fingerprint.py` | `Fingerprint` (one 5 s window: services, db, edges, slos, log_highlights, change_events); `.metrics()` → flat canonical keys; `TelemetrySource` protocol (`window`, `series`) | Telemetry → Brain, UI |
| C2 | `triage.py` | `TriageDraft` = exact LLM output (hypotheses, direction predictions per experiment, structured `confirms_if`); use `openai_schema.triage_response_format()` for OpenAI strict mode; validate with `.problems()`. `Verdict` = math output (diagnosis or `NONE_OF_THE_ABOVE`, support, observations with z-scores) | Brain → Orchestrator, UI |
| C3 | `levers.py` | `LeverAdapter` protocol (`catalog`, `estimate_blast_radius`, `apply(lever_id, params, ttl_s)`, `undo`, `status`); `CATALOG`: `retry_cap`, `shed`, `db_failover`, `canary_weight`; `Experiment` = apply → hold → undo | Adapters → Brain, Orchestrator |
| C4 | `audit.py` | `AuditEvent` (stage 1–8, kind, actor); `AuditSink` protocol, `JsonlSink`; ES index `faultline-audit`; `experiment_windows()` derives phase boundaries | Orchestrator → ES, UI |
| C5 | `fault.py` | Fault controller API (:9900) + `HttpFaultController`. **Hidden from Faultline** | Sandbox → `bench/` only |
| C6 | `clone.py` (approved by Owner 3) | Clone lab: `CloneLab` protocol + `HttpCloneLab` (:9910); `CloneSpec` = only observable config (versions, retry policy, workload rps, patch_ref); `CloneInfo.endpoints` exposes the same `/stats` + C3 control surfaces as production; `LAB_CATALOG`: `retry_policy`, `db_latency`, `db_capacity`, `cpu_limit`, `service_kill`, `service_restart`, all with `ttl_s`. **Never touches production; never exposes hidden state**; catalog wording is leak-checked | Sandbox (Owner 1) → investigators (Owner 3), orchestrator (Owner 4) |

`metrics.py` is the metric-key registry: `svc.<svc>.<field>`, `db.<field>`, `edge.<src>.<dst>.<field>`, `slo.<name>.value`.

Sandbox control endpoints for real levers (Owner 1 serves, Owner 4 calls) are defined in `contracts/README.md`: control service on :9901, `POST`/`DELETE /admin/{retry_override,shed,db/failover,canary}`, every `POST` takes `ttl_s`.

## Fakes — build against these until the real thing exists

- `fakes.FakeWorld` — seeded simulator of the hero sandbox; implements `TelemetrySource`, `LeverAdapter` and `FaultController` in one object. Worlds: storm (retry cap fixes it permanently), degraded_db (cap doesn't help, storm returns; failover heals), cpu_starve (fits neither → none-of-the-above). Time advances only via `world.advance(seconds)`.
- `fakes.FakeLeverAdapter` — records actions, injectable clock for TTL tests.
- `fakes.ReplayTelemetrySource` — replays fixture JSON.
- `contracts/fixtures/` — healthy/storm/degraded fingerprints, full experiment series for both worlds, sample triage, verdict, catalog, experiments, audit trail.

## Rules

- **Telemetry ownership:** Owner 2 owns the canonical observable `/stats` → C1 `Fingerprint` conversion in `faultline_telemetry.fingerprint_from_stats`, plus Elasticsearch persistence and queries. Product and integration code must call that builder rather than reimplementing counter deltas or metric semantics. Owner 4 owns the Product CLI/polling lifecycle and supplies Owner 2's store with completed windows, incident IDs, and clone IDs; it must not duplicate the telemetry conversion or Elasticsearch query logic.
- **Three environments (v6):** production (hidden cause; Faultline may only use C3 levers), clean clones (built only from observable config/versions/workload; C6 actions allowed), benchmark controller (C5; invisible to Faultline and investigators). Never copy hidden fault state into a clone.
- **Fairness (enforced by `tests/test_boundary.py`):** code under `faultline/` must never import `faultline_contracts.fault`; runtime code must not import `faultline_contracts.fakes` (tests may). Never put world/fault labels or trigger timing in telemetry.
- **Units:** `_ms`, `_qps`; rates/ratios in 0–1 except `retry_ratio`, which is attempts per logical request (1 = no retries, 4 = three retries). UTC timestamps. 5 s windows (`WINDOW_S`).
- **Missing data is omitted (`None`), never 0** — otherwise the judge reads a fake drop.
- **Don't hardcode latency thresholds.** Fixture and simulator magnitudes differ; derive baselines from the noise model (σ = max(std, 10% of typical), significant at ~3σ).
- Models use `extra="forbid"` and carry `schema_version`. Don't depend on event order within the same audit timestamp.
- **Changing contracts:** PR approved by the consuming owner. After model changes run `uv run python scripts/make_fixtures.py && uv run python scripts/export_schemas.py`; a test fails if schemas are stale.
- Every lever action must be reversible and carry a TTL. Action budget: 5 per incident, then page a human.
- **Clone lifecycle hardening (PR #33, now merged into this branch through main):** `cd sandbox && uv run python -m unittest discover -s tests -p 'test_lab_lifecycle.py' -v` checks lifetime/cleanup behavior without Docker. Defaults are `LAB_MAX_CLONES=2` (1–3), `LAB_CLONE_MAX_LIFETIME_S=3600` unrenewed; slots free only after successful teardown and failed cleanup retries about every 30 s. Product `--max-clones` defaults to 1 pending profiling. The older main snapshot above predates this merge.
- LLM calls use the OpenAI API (sponsor requirement). Log notable Codex usage for the Devpost write-up.
- Only measured numbers go on slides; say "handles performance and availability incidents", never "handles everything".

## Product UI: synthetic and read-only live modes

`product/ui/` is React/TypeScript + React Three Fiber. On main it supports explicitly synthetic scenarios plus read-only real incidents from the Product API: C4 audit JSONL and optional primary Elasticsearch C1 readings, streamed with Server-Sent Events. Synthetic examples remain the default; `?live` selects the newest recorded incident and `?incident=<id>` follows a specific incident. Neither mode executes infrastructure actions. The live API/SSE code is on main, not in the older base of the pending clone-lifecycle branch.

- From `product/ui/`: `npm ci`, then `npm run dev -- --port 4173 --strictPort` (tested with Node 24).
- On main, serve real incidents after building the UI: from `product/`, `uv run faultline ui --port 8010`; optionally pass `--extra-audit-log <path>`. The dev UI proxies `/api` to port 8010. Keep API keys server-side.
- Verification: `npm run build` (includes TypeScript), `npm test` (model/layout), and `npm run test:browser` (Playwright, currently configured for installed Google Chrome; starts or reuses the local server on port 4173).
- Keep frontend changes inside `product/ui/`; runtime Product adapters are being developed independently. Never expose backend credentials in frontend environment variables. Generated build, browser reports, and screenshots are ignored.

## Persistent knowledge

- `faultline/telemetry/scripts/es_doctor.py` is the read-only Elastic preflight: bounded `GET /_index_template/{index}` + `size: 1` `POST /{index}/_search` only, for primary `FAULTLINE_ELASTICSEARCH_*` and display mirror (`FAULTLINE_OBSERVABILITY_ELASTICSEARCH_*` preferred over `FAULTLINE_ELASTICSEARCH_MIRROR_*`). It never calls `client_from_env` (no mirror worker/outbox), never writes/refreshes, never touches Kibana/Agent Builder, and never uses `*_SETUP_API_KEY`. Run from `faultline/telemetry`: `uv run python scripts/es_doctor.py [--incident-id ID] [--require-mirror]`; exit 0 all requested checks present, 1 data/access/request checks need attention, 2 required config absent/incomplete/invalid. Sanitized JSON output; timestamp age is an observation, not delivery lag; a `forbidden` template GET reflects key privileges, not broken ingestion.
- Mirror queue admission is serialized across independent SQLite connections sharing one outbox: `MirroredElasticsearchClient.index` issues `BEGIN IMMEDIATE` before the capacity SELECTs, so a competing connection waits for the lock before checking capacity. If the 0.1 s SQLite timeout expires, its mirror enqueue is rejected (counted in health `rejected`, `last_error` `OperationalError`) while the already-successful primary write is retained. Run from `faultline/telemetry`: `uv run pytest -q tests/test_mirror.py tests/test_factory.py`. Mirror tests use `tmp_path` outboxes only — never the live default `~/.local/state/faultline` path.

## FAST prepared demo (worktree: distributed-demos)

Prepared patch snapshot (run from `sandbox/`):
`uv run python scripts/prepare_demo_patch.py --output <worktree>/.faultline/prepared-fast-<tag>` — writes `prepared-patch.json` (`faultline-prepared-patch/1`, `prepared:sha256:<digest>`, replay profile `retry-storm-v1`). Rejects symlinks; never mutates the source checkout.

Isolated dedicated lab (do NOT reuse the shared :9910 manager or `faultline-sandbox` project): run detached from `sandbox/` with
`LAB_PROJECT_PREFIX=faultline-demo-fast-<tag>- LAB_PORT_OFFSET=20000 LAB_MAX_CLONES=2 SANDBOX_APP_IMAGE=faultline-demo-base:local OTEL_SDK_DISABLED=true uv run uvicorn services.lab.app:app --host 127.0.0.1 --port 19910` — strip `FAULTLINE_ELASTICSEARCH_*`/`ELASTICSEARCH_*` env first. Base image: `docker build -f sandbox/Dockerfile -t faultline-demo-base:local <worktree>`; prebuild the patched image with `docker build -f <snapshot>/sandbox/Dockerfile -t <prefix>orders-v2:prepared <snapshot>`.

Harness (lead-owned): `uv run python integration/fast_demo.py --patch-context <snapshot> --lab-url http://127.0.0.1:19910 --env-file <path>` (drives `faultline prepared-watch` internally). Local UI evidence: `faultline ui --fingerprints-log <path>` selects `JsonlFingerprintStore` instead of Elasticsearch — explicit, never mixed.

## Advanced distributed stack (offline, NOT launched — Docker 7.7GiB is a hard blocker)

`cd sandbox && uv run --group advanced python -m advanced.cli plan|up|status|build` — `up`/`build` are dry-run unless `--execute`; private kubeconfig `.faultline/advanced/kubeconfig`, context `kind-faultline-advanced`, kind binary `.faultline/bin/kind` (v0.27.0, checksum-verified). Clone lab manager `sandbox/advanced/lab.py` binds 127.0.0.1:19920, requires the main cluster + `docker info` MemTotal ≥ 12GiB preflight, namespaces `faultline-advanced-clone-<8hex>` only, tokens in `.faultline/advanced/credentials/<clone_id>.token` (0600, never in C6 payloads). Control service `advanced/control.py` (Bearer `CONTROL_TOKEN`, HMAC compare) serves the C3 catalog + clone-only `/lab/actions`.

Status honesty: FAST timing unqualified until lead measurement report; advanced foundation/control/UI verified offline only — advanced reset is a declared 503 (measurement gate pending), `patch_ref` unsupported, Product advanced adapter/agent/case-driver unfinished, C1 `ResourceStats` requires consuming-owner approval before merge.

## Canary runtime + observation (canary-fix)

Envoy `runtime_modify` integer `FractionalPercent` units are out of **100**, not 10000 — `canary_weight` must write the explicit JSON form `{"numerator":N,"denominator":"TEN_THOUSAND"}` via `canary_runtime_value()` (shed stays integer percent). `LiveTelemetrySource.observe(duration_s, required_services=...)` collects fresh post-apply snapshot pairs at each 5 s boundary (full 120 s, required-service presence + producer-time monotonicity enforced; scrapes serialized against the poller via `_scrape_lock`). The prepared profile checks the measured split from C1 QPS integrals (estimated request counts, 3σ binomial band). Prior fast run `fast-8bad26cf028b` (281 s) remains FAILED/unmodified; fixed-version live rehearsal is pending the active exclusive comparison pilot — no Docker builds/clones while it runs. `integration/.gitignore` is restored; do not edit it.

## Comparison lab (bench/comparison-lab)

- `bench/src/faultline_bench/comparison.py` owns the versioned `faultline-comparison/1` recording and scoring protocol. Browser/API code only replays its stored results; it does not recompute evaluation scores.
- From `bench/`, `uv run faultline-compare --plan` is offline by default. The default development plan has two cases across Elastic, Observe, Probe, and Clone + Probe. `--cases case-a --arms observe probe` narrows iteration; `--include-healthy --cases case-c` selects the healthy control.
- `uv run faultline-compare --demo` writes a clearly synthetic recording under `runs/comparisons/`. The checked-in `product/ui/public/cmp-example.json` is an illustrative frozen fixture, not vendor output or accuracy evidence.
- Real execution requires `--execute --exclusive-sandbox`. This explicitly authorizes sandbox faults/resets and TTL-bound C3/C6 actions; do not run while any watch, demo, benchmark or another owner uses the target. Targets are restricted to loopback. Runs restore the original workload and stop the suite if owned-resource cleanup fails. A local target lock excludes other comparison runners, not unrelated responders.
- Elastic additionally requires `--allow-elastic-setup`. It creates a dedicated `faultline-comparison-windows` index and a run-scoped ES|QL tool before timing the incident. Its model calls use the stock `elastic-ai-agent` with only that read-only tool enabled. Comparison telemetry and tool/conversation artifacts remain in Elastic; no automatic cloud deletion occurs.
- Elastic configuration uses `KIBANA_URL`, `ELASTIC_AGENT_BUILDER_API_KEY`, `FAULTLINE_ELASTICSEARCH_URL`, `FAULTLINE_ELASTICSEARCH_API_KEY` and optional `FAULTLINE_AGENT_BUILDER_INFERENCE_ID` (default `faultline-openai-investigation`). Kibana and Elasticsearch must refer to the same deployment. Keys need tool-management/inference access and appropriate create/write/read access to the dedicated comparison index; an ingestion-only key is insufficient. Direct arms require `OPENAI_API_KEY`. No credentials are loaded into the frontend or copied into recordings.
- The comparison stops after diagnosis/reversible mitigation: no Devin, patch, canary, or Datadog execution. The clone arm uses the existing Product reproduction gate and can use its labelled seeded-recipe fallback; it does not implement clone-based planner re-ranking. Failed clone investigation never silently becomes the production-only comparison arm.
- Missing windows are not carried forward. Customer impact is a labelled C1 rate-integral estimate, not an exact request count. Recovery uses a sustained healthy interval with baseline-compatible errors and maintained request rate, and may be mitigation-supported. Read-only recovery is not applicable. Cost and incomplete token usage remain unknown. All outcomes are development-set results, including non-ignitions and responder failures.
- Focused checks: from `bench/`, `uv run pytest -q tests/test_comparison.py`; from `product/`, `uv run pytest -q tests/test_comparison_api.py`; from `product/ui/`, `npm run build`, `npm test -- src/comparison.test.ts`, `FAULTLINE_UI_PORT=4180 npx playwright test tests/comparison.spec.ts`.
- The UI comparison entry is `?compare`; `/api/comparisons` and `/api/comparisons/{id}` serve bounded local recordings read-only. The API directory defaults to this checkout's `runs/comparisons`; local JSON import also works. `FAULTLINE_UI_API_URL` optionally changes the Vite API proxy; no cloud credential belongs in it.

### What a confirms_if may confirm (judge/planner, post-Pilot 1)

Pilot 1's storm case reached `H_db confirmed` on a retry storm. Four gates now stand between measured evidence and a confirmed cause; `faultline/brain/tests/test_confirmation_gates.py` covers each one.

- **Tied support is decided by confirmation, not list order.** Support is compared within `judge.SUPPORT_TIE`, every tied hypothesis is tested, and a diagnosis stands only if exactly one of them passes. Pilot 1 tied at 0.500/0.500 and the leader was whichever hypothesis the model happened to list first; the hypothesis that had actually passed its test was never consulted.
- **A confirmation probe must separate the survivors.** `planner.separates` is required by both `confirmation_experiment` and the judge. An experiment every hypothesis expects to respond to identically confirms none of them: relieving a saturated dependency also relieves a caller that is saturating it. Pilot 1 confirmed on `db_failover`, which its own planner had scored separation 0.
- **A claimed recovery must survive release.** A `within_baseline` confirmation is void when that same experiment's measured after-release evidence mostly contradicts the hypothesis. Directional confirmations are exempt: the chaos suite showed the unrestricted rule also blocks the legitimate degraded-DB diagnosis, where the incident does not return inside one short watch window either.
- A single-hypothesis matrix is exempt from the separation requirement; there is nothing to separate from.

Evidence, not just tests: re-judging Pilot 1's recorded storm telemetry through `LiveBrain.judge` flips `retry_cap_0_20s` from `none_of_the_above` to `H_meta confirmed` (the correct cause, from the first zero-blast-radius probe) and `db_failover_30s` from `H_db confirmed` to `none_of_the_above`. These gates were derived from that one recorded case; passing it is not held-out evidence.

### Pilot 3 result (development set, two cases, tuned — not a benchmark)

`bench/pilot3-runs/cmp-a40d3af6352f492594c348cca129690a.json`. Four live runs, 300 s horizon, `gpt-4.1`, same injected conditions per case.

| | case-a (retry storm) | case-b (degraded DB) |
|---|---|---|
| observe (read-only) | `H_db` — wrong | `H_db` — correct |
| probe | `H_meta` — correct, 108 s | `H_db` — correct, 110 s |
| probe failed checkouts | 5,384 vs 22,645 read-only | 23,837 vs 23,137 read-only |
| probe recovery | recovered, 75 s | not recovered |
| tokens | probe 3,332 vs observe 237,118 | probe 3,751 vs observe 237,314 |

The read-only arm answers `H_db` in **both** worlds: from telemetry alone the storm looks like a DB problem, which is the same false reading Pilot 1's judge made. One retry-cap experiment separates them — healthy while capped, then *stays* healthy (storm) or the incident *returns* (degraded DB). Two reversible production actions per run, zero rollback failures.

Honest limits: n = 2, gates tuned on these cases, no held-out run. On case-b the probe arm diagnosed correctly but held `retry_cap` as its stopgap, which does not fix a degraded dependency, so it did not recover — the comparison harness does not run the durable-fix stages (6–8) that would.

### Loading a recording into the comparison UI

`faultline ui` serves every `cmp-*.json` in `<repo>/runs/comparisons` (no CLI flag; `create_app(..., comparison_dir=...)` if you need another directory). Copy, never symlink — the loader rejects symlinks and anything outside that directory.

```bash
cd product && uv run faultline ui --port 8010     # then open /?compare and pick the recording
```

Runs recorded before `RecordingAudit` carried `audit_detail` show the Faultline arms as bare one-line titles beside an observer arm quoting its whole answer, which understates the side that did the work. `bench/comparison_enrich.py` restores that detail from each run's own `audit.jsonl`, matched by the `c4:` reference already on every event, filling only empty fields and appending a protocol note saying it did:

```bash
cd bench && uv run --no-sync python comparison_enrich.py \
  --recording pilot3-runs/cmp-<id>.json --work pilot3-runs/.work --output ../runs/comparisons
```

The original recording stays untouched; the enriched copy is the one served. Live runs now carry the detail directly, so this is only needed for Pilots 1–3.

### Pilot 1 / offline observer regression gate

- Pilot 1 is harness-invalid for competitive claims: the user identified unequal JSON handling between direct OpenAI and Agent Builder, missing observer repair attempts, and insufficient error artifacts. Observer errors are not evidence of Elastic RCA failure. Keep Pilot 1 recordings unchanged; do not publish their error counts as diagnosis accuracy.
- The 300-second common horizon constrained clone investigation. Pilot 2 should use an explicitly recorded common `--horizon-s 600` for every arm, after the user releases the sandbox. This is a proposed next-pilot setting, not a change to Pilot 1 or a guarantee that all clone investigations finish.
- The user owns fixes to `comparison_agents.py`, `comparison_runtime.py`, `comparison_live.py` and `tests/test_comparison.py`; no new live execution until they signal the sandbox is free.
- Offline entry point lives at `bench/comparison_replay.py`, outside the source directories hashed by the running pilot. From `bench/`: `uv run --no-sync python comparison_replay.py --fake-world storm --scenarios plain` is a no-network smoke; omit `--scenarios` to check JSON framing, corrective re-asks, persistent invalid replies, provider exceptions and deadline errors across both observer arms.
- Replay captured data with `uv run --no-sync python comparison_replay.py --run-dir pilot-runs/.work/<run-id>`. It snapshots the input into a fresh output directory, invokes the real `worker_main` / `run_observer` / parser and provider adapters, replaces only provider transport and the clock, gates C1 windows by virtual availability, and blocks sockets, subprocesses, levers and clones. Original run artifacts are never overwritten. `--origin` overrides the default clock origin (first window start plus recorded baseline); `--start-s` overrides the virtual observer start, not a detection benchmark.
- The replay suite returns nonzero when an observer contract fails, with `replay-suite.json`, per-scenario `replay-report.json`, provider request transcripts and the actual worker artifacts. A repaired implementation should turn that offline gate green. Fake replies and virtual timings are never model accuracy, recovery or cost evidence; the schema is deliberately distinct from UI benchmark recordings.
- Focused harness checks: `uv run --no-sync pytest -q tests/test_comparison_replay.py`. These validate replay plumbing and isolation; passing them does not imply that the separate observer contract gate passed. Do not replace the user's red regression tests or fix runtime behavior in the replay adapter.
