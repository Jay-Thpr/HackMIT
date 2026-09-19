# Faultline — Persistent Project Knowledge

Last refreshed: 2026-09-19

This is the working context file for the hack. Keep it current as the project, decisions, ownership, and evidence evolve. The contracts are frozen unless a consuming owner approves a PR.

## Product in one paragraph

Faultline is an autonomous incident responder for OpenTelemetry-instrumented systems. It treats an incident as a causal question: when passive telemetry cannot distinguish plausible sustaining causes, it chooses the safest reversible intervention that separates them, measures the response against observed noise, and only then issues a diagnosis. It may retain a reversible mitigation and send a durable code fix through a verified canary. The governing principle is: **the LLM proposes and explains; measurement decides.**

## Hero scenario and expected result

The dashboard shows the same steady-state symptoms in two hidden worlds: roughly 320 DB queries/s, retry ratio around 4, saturated pool, high DB latency, and failing checkouts.

| Hidden sustaining cause | Retry-cap experiment: cap retries at 0 for ~20 s, then release | Interpretation |
| --- | --- | --- |
| Self-sustaining retry storm (`H_meta`) | Load and latency recover while capped, then remain healthy after release | The feedback loop was sustaining the incident; capping retries also breaks it. |
| Reduced DB capacity (`H_db`) | Load falls while capped but DB stays slow; on release, the storm returns | The DB degradation remains; try DB failover / remove the external load. |
| CPU-starved Payments (none of the above) | Neither hypothesis’s positive confirmation test passes | Return `none_of_the_above` and page a human. |

The diagnostic criterion is confirmation, not merely eliminating a rival. In particular, an `H_meta` verdict needs a post-release metric (normally DB query latency) within the healthy baseline.

## End-to-end pipeline

1. Ingest OTel telemetry, logs, and changes into Elasticsearch.
2. Detect SLO breach.
3. Triage via OpenAI structured output: hypotheses and directional predictions.
4. Plan and run a safe reversible experiment if triage is ambiguous.
5. Keep or undo mitigation based on measurement.
6. Request a durable patch through Devin.
7. Canary, compare, then promote or revert.
8. Produce a timeline, evidence, actions, and PR report.

The demo’s centerpiece is one live chart of DB query latency and request load: cap retries, recover, release, and observe the health persist (or not). The UI must separate LLM reasoning from measured evidence.

## Ownership and boundaries

| Owner | Directory / responsibility |
| --- | --- |
| 1 | `sandbox/`: Docker target system, Envoy, load generator, storm gate, hidden fault controller |
| 2 | `faultline/telemetry/`: OTel → Elasticsearch, fingerprints, ES audit sink |
| 3 (this checkout’s active scope) | `faultline/brain/` and `bench/`: OpenAI triage, noise, planner, judge, benchmarks. User is implementing C2; assistant support must stay strictly within this scope. |
| 4 | `faultline/{adapters,orchestrator,cli,ui}/`: action/code adapters, state machine, Devin, canary, product surfaces |
| Shared | `contracts/`: C1–C5 interfaces, fixtures, fakes, schemas, tests |

Faultline may touch a target system only through telemetry and action adapters. Runtime code under `faultline/` must not import `faultline_contracts.fault` or `faultline_contracts.fakes`; C5 and `FakeWorld` are for sandbox, bench, and tests. Never leak world labels, fault labels, or trigger timing into telemetry, fingerprints, logs, or audit events.

## Contract reference

| Contract | Important shape / rule |
| --- | --- |
| C1 `Fingerprint` / `TelemetrySource` | Fixed five-second UTC windows. `Fingerprint.metrics()` exposes canonical `svc.*`, `db.*`, `edge.*`, and `slo.*` keys; missing values are omitted, never replaced with zero. |
| C2 triage / verdict | `TriageDraft` is strict OpenAI output: hypotheses, per-hypothesis/per-experiment predictions, and `confirms_if`. Validate it with `.problems()` before creating `TriageResult`. `Verdict` is math output with normalized support and z-scored observations. |
| C3 levers | `LeverAdapter` catalog, blast-radius estimate, apply, undo, and status. Every action is reversible and must include a target-enforced TTL. Standard levers: retry cap, shed, DB failover, canary weight. |
| C4 audit | All transitions and actions are `AuditEvent`s. `experiment_start` and `experiment_end` in the audit log are the source of truth for judge phase boundaries. ES index: `faultline-audit`. |
| C5 hidden fault control | Sandbox/bench only on port 9900: storm, degraded DB, CPU starvation, reset/state. It is intentionally not re-exported by `faultline_contracts`. |

Conventions: pydantic v2 models reject extra fields; top-level payloads have `schema_version="1"`; timestamps are UTC-aware; `_ms` means milliseconds; ratios/rates are 0–1 except `retry_ratio` (attempts per logical request); `blast_radius_pct` is 0–100.

The real action-control surface belongs to Owner 1 at port 9901. It offers idempotent deletes plus target-side TTL reversion for retry overrides, shedding, DB failover, and canary weight.

## Brain implementation status

Existing code in `faultline/brain/`:

- `telemetry.py`: C1 readers and a fairness guard that rejects forbidden hidden-label substrings.
- `noise.py`: `NoiseModel` computes a per-metric mean baseline and `sigma = max(population_std, 10% * abs(mean))`; directions are flat unless `|z| >= 3`.
- `judge.py`: uses audit-derived experiment windows, incident baseline for the during phase, healthy baseline for after-release, direction-agreement support scoring, and requires the leading hypothesis to pass its own confirmation test before diagnosis.
- Tests cover telemetry fairness, the noise model, and the storm verdict from shared fixtures.

Implemented in the Brain scope: an injected OpenAI-compatible triage client with strict-schema semantic-validation retry; a planner that ranks C3 experiments by direction separation minus blast-radius cost; and a deterministic active C2 benchmark runner in `bench/`.

Still to implement: passive-only, LLM-only, nearest-centroid, and random-lever benchmark baselines, plus aggregate benchmark reporting. The active runner confirms the storm with the zero-blast retry-cap experiment. In the current reduced-DB simulator run, retry cap alone safely returns `none_of_the_above` rather than claiming an unconfirmed DB diagnosis; a multi-experiment orchestration policy should schedule the DB-failover probe next.

## Fakes, fixtures, and verification

`FakeWorld` is a deterministic discrete-time simulator implementing telemetry, levers, and the hidden fault controller. It validates the important behavior: the storm persists after a transient trigger, retry capping permanently breaks only that storm, failover heals a reduced-capacity DB only while applied, and CPU starvation confirms neither hero hypothesis.

Key fixture timeline: healthy for 60 s, incident for 60 s, retry cap from 15:02:00 to 15:02:20 UTC, then a 30 s watch period. Fixtures are generated deterministically and schemas must stay synchronized with models.

Useful checks:

```bash
cd contracts && uv run pytest -q
uv run python examples/run_hero.py
cd ../faultline/brain && uv run pytest -q
```

## Decision and demo guardrails

- No hard-coded latency thresholds; derive baselines and significance from healthy telemetry.
- Action budget is five per incident, after which Faultline pages a human.
- Automatically perform telemetry reads and reversible actions; code changes are canary-gated; irreversible actions are prohibited.
- Auto-undo on regression, retain a kill switch, and audit every relevant action.
- Build the core loop first. Cut in order: OTel Demo suite, Elastic extras, easy case, live canary, live Devin. Never cut the storm experiment, mathematical judge, chart, or benchmark.
- Only report measured numbers. Claim coverage for performance and availability incidents, not every kind of incident.

## Graphify readout

The generated graph reports 398 nodes, 889 edges, and 16 communities. Its useful navigation groups align with the architecture: C1/C2 specs, audit/base models, fake telemetry, fake levers/simulator, OpenAI strict schema, fairness/benchmarks, autonomy guardrails, pipeline stages, brain math, and telemetry ingestion. Treat the graph’s inferred semantic edges cautiously; its own report marks 102 inferred edges and one ambiguous fallback-hero/easy-case relationship. Source contracts and tests outrank inferred graph links.

## Repository and workflow

- Canonical remote: `https://github.com/Jay-Thpr/HackMIT.git`.
- Local-first: do not deploy, merge, push, or open a PR unless requested.
- Never merge, force-push, rebase a pushed branch, or push to `main`.
- `contracts/` is frozen absent consuming-owner approval and the required schema/fixture regeneration.
- Capture notable Codex work with timestamps for the OpenAI/Devpost story.

## Open items from the PRD

- Team size confirmed: four total. Submission deadline remains to be confirmed.
- Confirm HackMIT rules for reused open-source code and credit any reuse.
- Verify Devin API access with event credentials.
- Confirm the OpenTelemetry Demo runs on a team laptop, or explicitly drop it.
- Pass the early storm gate (five reliable runs) and ambiguity check before investing in polish.
