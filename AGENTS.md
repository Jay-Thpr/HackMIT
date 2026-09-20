# AGENTS.md — Faultline

Read this first. Then `PRD.md` (product plan, v6: adds the clone lab) and `contracts/README.md` (interface spec).

## What Faultline is

An autonomous incident responder. When telemetry can't distinguish causes that fit the same symptoms, Faultline runs a safe, reversible experiment on the live system to tell them apart. Hero case: self-sustaining retry storm vs. degraded DB — cap retries; if the system stays healthy after release it was a storm, if the storm returns the DB is degraded. **The LLM proposes and explains; measurement against noise decides.**

## Repo layout

| Path | Owner | Status |
|---|---|---|
| `contracts/` | shared | Built — interfaces C1–C5, fakes, fixtures, schemas, tests |
| `sandbox/` | 1 Sandbox + storm | Built — Docker Compose target system, Envoy, fault controller :9900, levers :9901, clone lab manager :9910 (`uv run uvicorn services.lab.app:app --port 9910`, host process); see `sandbox/INTEGRATION.md` |
| `faultline/telemetry/` | 2 Telemetry + Elastic | Built — `/stats` poller → C1, ES fingerprint store (`faultline-fingerprints`), ES audit sink, analytics, ES|QL `incident_timeline`, index templates; Elastic Cloud via `FAULTLINE_ELASTICSEARCH_URL` + `FAULTLINE_ELASTICSEARCH_API_KEY` (see `.env.example`); smoke: `cd faultline/telemetry && uv run python scripts/es_smoke.py` |
| `faultline/brain/` | 3 Brain | Built — strict OpenAI triage (`triage.py`), noise model (σ = max(std, 10% typical)), math judge with `confirms_if` gating, experiment planner (separation − blast radius), `CloneInvestigator` + agentic `InvestigatorAgent`/`AgenticCloneInvestigator` (`investigator_agent.py`: LLM proposes C6 actions with predictions under a budget, math scores reproduction/recovery/prediction at ≥75 %; live-verified on Docker clones, `live-storm-035001`), read-only Elastic Agent Builder investigation agent (`elastic_investigation.py`). **Direction (PRD v6.2):** the Brain's LLM roles run as OpenAI behind Elastic Agent Builder reasoning over ES-stored context via closed read-only tools; direct OpenAI is the fallback; the judge still decides. See `C2_HANDOFF.md`, `C2_BRAIN_GUIDE.md`. Tests: `cd faultline/brain && uv run pytest -q` |
| `product/ (faultline_product)` | 4 Product | Built — orchestrator (8 stages + 4a investigators + 6b clone verification + Devin revise loop), C3/C1/C6 adapters, Devin API v3 adapter, git checkout of PR/branch patches, CLI; all run live incl. real OpenAI + Devin (`live-13`). UI in progress (separate). Live command: `faultline watch --telemetry sandbox --levers sandbox --brain live --lab-url http://127.0.0.1:9910 [--devin]`; keys via `~/.config/faultline/env` |
| `bench/` | 3 Brain | Built on `FakeWorld` — deterministic active runner, baselines (passive-only, LLM-only, nearest-centroid, random-lever), frozen-suite orchestration with JSON aggregate report. Not yet built: live-sandbox driver (PRD Lane C2) and the clone arm (C3). Tests: `cd bench && uv run pytest -q` |
| `integration/` | shared | Built — bench-side harnesses against the live stack: `smoke_sandbox.py` (C5 + `:9901` + `/stats` hero sequence, `--cpu`, `--repeat N`) and `live_loop.py storm|degraded` (C5 inject → real `faultline watch` → C4 audit assertions); `demo.py storm|degraded` (presenter driver). **Do not `compose down`/`up --build` production while these or the benchmark run.** Observes only; never retunes `sandbox/`. Also `chaos/`: deterministic chaos suite — real `Orchestrator` + real Brain math over `FakeWorld` (no Docker), ~38 cases × 3 seeds covering hidden-world sweeps, compound/mid-run faults, and responder-dependency failures (telemetry gaps/lag, refused/no-op/sticky levers, garbage LLM output, lab/Devin/canary down, budget); every run checks the safety invariants in `chaos/harness.py`, known gaps are `xfail(strict)`. Run: `cd integration && uv run pytest -q tests/test_chaos.py` or `uv run python -m chaos` (table + JSON in `runs/`). **`uv run pytest -q` with no path also runs `test_smoke_sandbox.py`, which injects C5 faults into production whenever the sandbox is up — never run it while a live `watch`/`demo.py` is in progress** |

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
- **Units:** `_ms`, `_qps`, ratios/rates in 0–1. UTC timestamps. 5 s windows (`WINDOW_S`).
- **Missing data is omitted (`None`), never 0** — otherwise the judge reads a fake drop.
- **Don't hardcode latency thresholds.** Fixture and simulator magnitudes differ; derive baselines from the noise model (σ = max(std, 10% of typical), significant at ~3σ).
- Models use `extra="forbid"` and carry `schema_version`. Don't depend on event order within the same audit timestamp.
- **Changing contracts:** PR approved by the consuming owner. After model changes run `uv run python scripts/make_fixtures.py && uv run python scripts/export_schemas.py`; a test fails if schemas are stale.
- Every lever action must be reversible and carry a TTL. Action budget: 5 per incident, then page a human.
- Clone lab ops: `cd sandbox && uv run python -m unittest discover -s tests -p 'test_lab_lifecycle.py' -v` (stdlib, no Docker). Lab defaults: `LAB_MAX_CLONES=2` (1–3), `LAB_CLONE_MAX_LIFETIME_S=3600` per clone, unrenewed; a slot frees only after teardown succeeds and failed cleanup retries ~30 s. Product `--max-clones` keeps default 1 pending profiling.
- LLM calls use the OpenAI API (sponsor requirement). Log notable Codex usage for the Devpost write-up.
- Only measured numbers go on slides; say "handles performance and availability incidents", never "handles everything".

## Product UI prototype

`product/ui/` is a standalone React/TypeScript + React Three Fiber design prototype with explicitly simulated data. It does not connect to the sandbox or execute infrastructure actions. The agreed design is in PRD.md under "Product UI: spatial investigation workspace".

- From `product/ui/`: `npm ci`, then `npm run dev -- --port 4173 --strictPort` (tested with Node 24).
- Verification: `npm run build` (includes TypeScript), `npm test` (model/layout), and `npm run test:browser` (Playwright, currently configured for installed Google Chrome; starts or reuses the local server on port 4173).
- Keep frontend changes inside `product/ui/`; runtime Product adapters are being developed independently. Never expose backend credentials in frontend environment variables. Generated build, browser reports, and screenshots are ignored.

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
