# AGENTS.md — Faultline

Read this first. Then `PRD.md` (product plan, v6: adds the clone lab) and `contracts/README.md` (interface spec).

## What Faultline is

An autonomous incident responder. When telemetry can't distinguish causes that fit the same symptoms, Faultline runs a safe, reversible experiment on the live system to tell them apart. Hero case: self-sustaining retry storm vs. degraded DB — cap retries; if the system stays healthy after release it was a storm, if the storm returns the DB is degraded. **The LLM proposes and explains; measurement against noise decides.**

## Agent Builder runtime branch

`brain/agent-builder-runtime` adds opt-in Agent Builder triage and clone proposals with explicit inference routing, existing schema/semantic validation and audited direct-OpenAI fallback. The math judge, action budget and C3/C6 execution remain unchanged. Default reasoning remains direct OpenAI.

This branch incorporates main through `b805af8`, preserving paging, resume, stale-telemetry checks and similar-incident audit data. Agent Builder errors now retain sanitized categories and HTTP status in provenance; remote bodies, keys and URLs are not logged.

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
| `product/ (faultline_product)` | 4 Product | Built — orchestrator (8 stages + 4a investigators + 6b clone verification + Devin revise loop), C3/C1/C6 adapters, Devin API v3 adapter, git checkout of PR/branch patches, CLI; all run live incl. real OpenAI + Devin (`live-13`). Main also includes a read-only incident API (`faultline ui`) and SSE-backed live UI alongside synthetic scenarios; no browser infrastructure actions. Live command: `faultline watch --telemetry sandbox --levers sandbox --brain live --lab-url http://127.0.0.1:9910 [--devin]`; keys via `~/.config/faultline/env` |
| `bench/` | 3 Brain | Built — deterministic `FakeWorld` runner, passive-only/LLM-only/nearest-centroid/random-lever baselines, frozen simulator suite, and live-sandbox driver (`uv run faultline-bench-live`). The live clone-ablation arm and a completed frozen live benchmark remain outstanding in the checked handoff. Driver existence is not accuracy evidence. See `bench/README.md`; offline tests: `cd bench && uv run pytest -q`. |
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
- **Units:** `_ms`, `_qps`; rates/ratios in 0–1 except `retry_ratio`, which is attempts per logical request (1 = no retries, 4 = three retries). UTC timestamps. 5 s windows (`WINDOW_S`).
- **Missing data is omitted (`None`), never 0** — otherwise the judge reads a fake drop.
- **Don't hardcode latency thresholds.** Fixture and simulator magnitudes differ; derive baselines from the noise model (σ = max(std, 10% of typical), significant at ~3σ).
- Models use `extra="forbid"` and carry `schema_version`. Don't depend on event order within the same audit timestamp.
- **Changing contracts:** PR approved by the consuming owner. After model changes run `uv run python scripts/make_fixtures.py && uv run python scripts/export_schemas.py`; a test fails if schemas are stale.
- Every lever action must be reversible and carry a TTL. Action budget: 5 per incident, then page a human.
- **Pending clone lifecycle hardening (PR #33, not main at this snapshot):** `cd sandbox && uv run python -m unittest discover -s tests -p 'test_lab_lifecycle.py' -v` checks branch-only lifetime/cleanup behavior without Docker. This branch defaults to `LAB_MAX_CLONES=2` (1–3), `LAB_CLONE_MAX_LIFETIME_S=3600` unrenewed; slots free only after successful teardown and failed cleanup retries about every 30 s. Do not assume these safeguards exist on main until merged. Product `--max-clones` defaults to 1 pending profiling.
- LLM calls use the OpenAI API (sponsor requirement). Log notable Codex usage for the Devpost write-up.
- Only measured numbers go on slides; say "handles performance and availability incidents", never "handles everything".

## Product UI: synthetic and read-only live modes

`product/ui/` is React/TypeScript + React Three Fiber. On main it supports explicitly synthetic scenarios plus read-only real incidents from the Product API: C4 audit JSONL and optional primary Elasticsearch C1 readings, streamed with Server-Sent Events. Synthetic examples remain the default; `?live` selects the newest recorded incident and `?incident=<id>` follows a specific incident. Neither mode executes infrastructure actions. The live API/SSE code is on main, not in the older base of the pending clone-lifecycle branch.

- From `product/ui/`: `npm ci`, then `npm run dev -- --port 4173 --strictPort` (tested with Node 24).
- On main, serve real incidents after building the UI: from `product/`, `uv run faultline ui --port 8010`; optionally pass `--extra-audit-log <path>`. The dev UI proxies `/api` to port 8010. Keep API keys server-side.
- Verification: `npm run build` (includes TypeScript), `npm test` (model/layout), and `npm run test:browser` (Playwright, currently configured for installed Google Chrome; starts or reuses the local server on port 4173).
- Keep frontend changes inside `product/ui/`; runtime Product adapters are being developed independently. Never expose backend credentials in frontend environment variables. Generated build, browser reports, and screenshots are ignored.
