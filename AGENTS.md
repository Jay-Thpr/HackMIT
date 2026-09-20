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
| `faultline/brain/` | 3 Brain | Not started — OpenAI triage, noise model, planner, judge |
| `product/ (faultline_product)` | 4 Product | In progress — orchestrator, adapters, Devin adapter, CLI; UI not started |
| `bench/` | 3 Brain | Not started — benchmark runner and baselines |
| `integration/` | shared | Built — bench-side harnesses against the live stack: `smoke_sandbox.py` (C5 + `:9901` + `/stats` hero sequence, `--cpu`, `--repeat N`) and `live_loop.py storm|degraded` (C5 inject → real `faultline watch` → C4 audit assertions). **Do not `compose down`/`up --build` production while these or the benchmark run.** Observes only; never retunes `sandbox/` |

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
- LLM calls use the OpenAI API (sponsor requirement). Log notable Codex usage for the Devpost write-up.
- Only measured numbers go on slides; say "handles performance and availability incidents", never "handles everything".

## Product UI prototype

`product/ui/` is a standalone React/TypeScript + React Three Fiber design prototype with explicitly simulated data. It does not connect to the sandbox or execute infrastructure actions. The agreed design is in PRD.md under "Product UI: spatial investigation workspace".

- From `product/ui/`: `npm ci`, then `npm run dev -- --port 4173 --strictPort` (tested with Node 24).
- Verification: `npm run build` (includes TypeScript), `npm test` (model/layout), and `npm run test:browser` (Playwright, currently configured for installed Google Chrome; starts or reuses the local server on port 4173).
- Keep frontend changes inside `product/ui/`; runtime Product adapters are being developed independently. Never expose backend credentials in frontend environment variables. Generated build, browser reports, and screenshots are ignored.
