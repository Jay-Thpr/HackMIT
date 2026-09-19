# faultline-contracts

The shared interfaces between the four Faultline workstreams. **This README is the spec.** Code in
`src/faultline_contracts/` is the source of truth for shapes; `schema/` holds generated JSON Schema;
`fixtures/` holds valid example payloads for every contract. Build against the fakes and fixtures
until the real producer exists.

| Contract | Module | Producer → Consumer | Owner |
| --- | --- | --- | --- |
| C1 Fingerprint + `TelemetrySource` | `fingerprint.py`, `metrics.py` | Telemetry adapter → detector, triage, judge, UI | Owner 2 |
| C2 Triage / predictions / verdict | `triage.py`, `openai_schema.py` | Triage (LLM) → planner/judge (math) → orchestrator, UI | Owner 3 |
| C3 Levers (`LeverAdapter`) | `levers.py` | Action adapter → planner, orchestrator | Owner 4 (sandbox endpoints: Owner 1) |
| C4 Audit events | `audit.py` | Orchestrator → judge (phase boundaries), UI, report | Owner 4 (ES sink: Owner 2) |
| C5 Fault controller | `fault.py` | Bench / demo script → sandbox. **Hidden from Faultline** | Owner 1 |
| C6 Clone lab (`CloneLab`) | `clone.py` | Clone manager (sandbox) → investigators (Owner 3), orchestrator (Owner 4) | Owner 1 (consumer approval: Owner 3) |

## Install

```bash
cd contracts && uv sync            # dev env; run tests with: uv run pytest -q
```

Depend on it from another uv project (e.g. `faultline/`, `sandbox/`, `bench/`):

```bash
uv add --editable ../contracts
```

which is equivalent to this in `pyproject.toml`:

```toml
[project]
dependencies = ["faultline-contracts"]

[tool.uv.sources]
faultline-contracts = { path = "../contracts", editable = true }
```

Import as `from faultline_contracts import Fingerprint, TriageDraft, ...`. Python >= 3.11, pydantic v2.

## Conventions

- **Units** are in the field name: `*_ms` milliseconds, `*_qps` / `qps` per second, `*_ratio` / `*_rate` in [0, 1].
  Exception: `retry_ratio` is attempts per logical request (1.0 = no retries, 4.0 = every call retried 3 times).
  `blast_radius_pct` is a percentage 0–100.
- **Time**: timezone-aware UTC `datetime` everywhere; ISO-8601 with `Z` on the wire.
- **Windows**: telemetry is aggregated in fixed `WINDOW_S = 5` second windows, `[window_start, window_end)`.
- **Missing data is omitted** (`None` / absent key), never reported as 0. `Fingerprint.metrics()` skips it.
- **`schema_version`** (`"1"`) is on every top-level payload (Fingerprint, TriageResult, Verdict, AuditEvent).
  Bump it on any breaking change.
- **`extra="forbid"`** on every model: unknown fields are a validation error, so nothing leaks in silently
  (including hidden world labels).
- **Change process**: contract changes go through a PR that the *consuming* owner approves. Regenerate
  `schema/` and `fixtures/` in the same PR (`uv run python scripts/export_schemas.py`,
  `uv run python scripts/make_fixtures.py`); `tests/test_models.py` fails if `schema/` is stale.

## Metric key registry (`metrics.py`)

Predictions, verdicts and SLOs refer to metrics by canonical key. `Fingerprint.metrics()` flattens a window
to exactly these keys.

| Pattern | Fields |
| --- | --- |
| `svc.<service>.<field>` | `qps`, `p50_ms`, `p99_ms`, `error_rate`, `retry_ratio`, `timeout_rate` |
| `db.<field>` | `qps`, `query_p50_ms`, `query_p99_ms`, `pool_busy_ratio` |
| `edge.<src>.<dst>.<field>` | `qps`, `p99_ms`, `error_rate` |
| `slo.<name>.value` | the SLO's current value |

Service/SLO names are free-form (`[a-z0-9_-]+`, so the OTel Demo works); fields are fixed. Sandbox names:
services `gateway`, `orders`, `payments`, `fraud_check`; edges `gateway→orders`, `orders→payments`,
`payments→db`, `payments→fraud_check`; SLO `checkout` on `svc.gateway.p99_ms`, threshold 1000 ms.
Use `is_valid_metric_key(k)` / `unknown_metrics(keys, known)` to check.

## C1 — Fingerprint (Owner 2 → everyone)

One 5 s window of telemetry: `services: dict[str, ServiceStats]`, `db: DbStats`, `edges: list[Edge]`,
`slos: list[SloStatus]`, `log_highlights: list[LogHighlight]` (clustered templates + counts, not raw lines),
`change_events: list[ChangeEvent]` (deploys/config/flags; empty in the hero).

```python
class TelemetrySource(Protocol):
    def window(self, start: datetime, end: datetime) -> Fingerprint: ...
    def series(self, start: datetime, end: datetime, step_s: int = WINDOW_S) -> list[Fingerprint]: ...
```

```python
fp = telemetry.window(t - timedelta(seconds=5), t)
m = fp.metrics()                      # {"db.qps": 318.2, "svc.orders.retry_ratio": 3.9, ...}
breached = [s for s in fp.slos if s.breached]
```

## C2 — Triage, predictions, verdict (Owner 3)

- **`TriageDraft`** — exactly what the LLM returns (strict structured output: no defaults, no free-form dicts):
  `ambiguous`, `reasoning`, `hypotheses: list[Hypothesis]`, `predictions: list[Prediction]`.
  A `Prediction` is per hypothesis × experiment: `during` / `after_release` lists of
  `MetricExpectation(metric, direction: up|down|flat)` plus `confirms_if: Confirmation(phase, metric,
  expect: within_baseline|up|down|flat)`.
- **`TriageResult`** = `TriageDraft` + `incident_id`, `created_at`, `schema_version` (added by our code, not the LLM).
- **`draft.problems(known_metrics, experiment_ids)`** — semantic checks beyond the schema (unknown/malformed
  metric keys, predictions for unknown hypotheses or experiments, duplicate ids, reserved `none_of_the_above`).
  Empty list = OK. On problems, re-ask the LLM with the list.
- **`Verdict`** (math output): `diagnosis` (a hypothesis id or `NONE_OF_THE_ABOVE`), `confirmed`,
  `support: list[HypothesisSupport]` (sums to 1), `observations: list[Observation]` with
  `baseline, measured, sigma, z = (measured - baseline) / sigma, direction` (flat if |z| < 3).
  None-of-the-above rule: no hypothesis passed its own confirmation test.

OpenAI usage (the `openai` package is *not* a dependency of this package):

```python
from openai import OpenAI
from faultline_contracts import TriageResult, TriageDraft
from faultline_contracts.openai_schema import triage_response_format

resp = OpenAI().chat.completions.create(
    model="gpt-4.1",
    messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": fp.model_dump_json()}],
    response_format=triage_response_format(),   # strict json_schema built from TriageDraft
)
draft = TriageDraft.model_validate_json(resp.choices[0].message.content)
errs = draft.problems(known_metrics=set(fp.metrics()), experiment_ids={e.id for e in candidates})
result = TriageResult(**draft.model_dump(), incident_id=incident_id)
```

## C3 — Levers (Owner 4; sandbox endpoints Owner 1)

Every experiment, mitigation and code rollout goes through a `LeverAdapter`:

```python
class LeverAdapter(Protocol):
    def catalog(self) -> list[LeverSpec]: ...
    def estimate_blast_radius(self, lever_id: str, params: dict) -> float: ...   # % of successful requests
    def apply(self, lever_id: str, params: dict, ttl_s: int) -> ActionHandle: ...
    def undo(self, handle: ActionHandle) -> ActionHandle: ...
    def status(self, handle: ActionHandle) -> ActionStatus: ...   # active | undone | expired | failed
```

- `CATALOG` (also `fixtures/catalog.json`): `retry_cap {max_retries: 0..3}`, `shed {fraction: 0..1}`,
  `db_failover {}`, `canary_weight {v2_weight: 0..1}`. `params_schema` is JSON Schema for the params;
  `max_ttl_s` bounds `ttl_s`. Bad lever/params or a refusal raises `LeverError`.
- `standard_blast_radius()` gives the default estimates: retry_cap 0%, shed `fraction*100`, db_failover 1%,
  canary `v2_weight*100`.
- An `Experiment` (`id`, `lever_id`, `params`, `hold_s`, `blast_radius_pct`) = apply → hold `hold_s` → undo →
  watch. Candidates in `fixtures/experiments.json`: `retry_cap_0_20s`, `shed_10_20s`, `shed_50_20s`, `db_failover_30s`.
- **Dead-man switch**: every apply carries `ttl_s`; the *target* reverts on its own when it expires, so a
  crashed Faultline never leaves retries capped or traffic shed. `ActionHandle.undo` (`UndoSpec`) is
  serializable so a fresh process can undo.

```python
h = levers.apply("retry_cap", {"max_retries": 0}, ttl_s=60)
time.sleep(20)
h = levers.undo(h)            # h.status == ActionStatus.undone
```

### Sandbox control service (`http://localhost:9901`) — what the real adapter calls

One small FastAPI service owned by Owner 1 fronts all levers (it forwards to Orders' override endpoint,
Envoy's admin/runtime API, the batch job, and Envoy cluster weights). All bodies/returns are JSON.
Every `POST` takes `ttl_s` (required, 1 ≤ ttl_s ≤ lever `max_ttl_s`) and the service reverts the lever itself
when it expires. `POST` replaces any active setting of the same lever and restarts its ttl.

| Lever | Apply | Undo |
| --- | --- | --- |
| `retry_cap` | `POST /admin/retry_override {"max_retries": 0, "ttl_s": 60}` | `DELETE /admin/retry_override` |
| `shed` | `POST /admin/shed {"fraction": 0.1, "ttl_s": 60}` | `DELETE /admin/shed` |
| `db_failover` | `POST /admin/db/failover {"ttl_s": 90}` | `DELETE /admin/db/failover` |
| `canary_weight` | `POST /admin/canary {"v2_weight": 0.05, "ttl_s": 1800}` | `DELETE /admin/canary` (v2 weight → 0) |

- Success: `200 {"lever_id": ..., "params": {...}, "applied_at": "<iso>", "expires_at": "<iso>", "active": true}`.
  `DELETE` returns the same shape with `"active": false` and is idempotent (200 even if nothing active).
- `GET /admin/levers` → `{"<lever_id>": {"active": bool, "params": {...}, "expires_at": "<iso>|null"}}`;
  the adapter's `status()` uses it (`active` → `active`, inactive after ttl → `expired`).
- Errors: `400` bad params / ttl out of range, `409` refused (e.g. canary without an orders-v2 build),
  `5xx` target unreachable. The adapter maps all of these to `LeverError`.
- `GET /healthz` → `200`.

## C4 — Audit events (Owner 4; Elasticsearch sink Owner 2)

`AuditEvent(incident_id, ts, stage: Stage 1..8, kind: EventKind, actor: llm|math|adapter|orchestrator|human,
summary, payload, action_id?, experiment_id?)`. Every stage transition, lever apply/undo, refusal and verdict
is an event. ES index: `AUDIT_INDEX = "faultline-audit"`.

The audit log is also **the source of truth for experiment phase boundaries**: `experiment_start` = lever
applied, `experiment_end` = lever released. The judge calls `experiment_windows(events)` to get
`ExperimentWindow(experiment_id, start, release)`. Always set `experiment_id` on those events and on the
matching `action_apply` / `action_undo`, and `action_id` on anything tied to an `ActionHandle`.

```python
class AuditSink(Protocol):
    def write(self, event: AuditEvent) -> None: ...
    def query(self, incident_id: str) -> list[AuditEvent]: ...   # sorted by ts

sink = JsonlSink("runs/audit.jsonl")          # local; the ES sink implements the same protocol
sink.write(AuditEvent(incident_id=iid, stage=Stage.experiment, kind=EventKind.experiment_start,
                      actor=Actor.orchestrator, summary="retry cap 0 for 20s",
                      action_id=h.action_id, experiment_id="retry_cap_0_20s"))
wins = experiment_windows(sink.query(iid))
```

Typical hero trail (`fixtures/audit_hero.jsonl`): detect → triage → experiment_start → action_apply →
action_undo → experiment_end → verdict → mitigation → patch_opened → canary_update → report.

## C5 — Fault controller (Owner 1; hidden from Faultline)

Used only by the sandbox, `bench/` and the demo script. HTTP API on `http://localhost:9900`
(client: `faultline_contracts.fault.HttpFaultController`):

| Request | Body | World |
| --- | --- | --- |
| `POST /fault/storm` | `StormFault {delay_ms: 800, duration_s: 20}` | A: transient DB delay → self-sustaining retry storm |
| `POST /fault/degrade_db` | `DegradeDbFault {capacity_qps: 40}` | B: batch job cuts DB capacity |
| `POST /fault/cpu_starve` | `CpuStarveFault {service: "payments", cpus: 0.1}` | none-of-the-above |
| `POST /fault/reset` | — | back to healthy |
| `GET /fault/state` | — | → `FaultState {world, active, params, started_at}` |

All endpoints return `FaultState`. Port 9900 must not be reachable from Faultline's config.

## C6 — Clone lab (Owner 1 serves; Owner 3 investigators and Owner 4 orchestrator call) — DRAFT, awaiting Owner 3 approval

A **clone** is a disposable, healthy replica of the target (its own Compose project and network) where an
investigator may run experiments that are far too aggressive for production. Three environments, never mixed:

| Environment | Knows the hidden cause? | Allowed actions |
| --- | --- | --- |
| Production | yes, invisible to Faultline | C3 levers only, via `:9901` |
| Clean clone | no; starts healthy | C3 levers via its own control service **plus** C6 `LAB_CATALOG` |
| Benchmark controller | yes (it injected it) | C5 on production only; unreachable from clones and investigators |

**What a clone inherits** (`CloneSpec`): a label, service `versions`, `retry_policy {max_retries, timeout_ms}`,
`workload {rps}` (reconstructed from observed production load) and an optional `patch_ref` for the orders-v2 slot.
That is all Faultline may legitimately know. It never inherits fault-controller state, `io_profile` contents or
production DB contents; `extra="forbid"` rejects anything else. A clone is `ready` only after a verified healthy
5 s window, and `reset()` gets it back there (503 if it can't).

**Same surfaces as production.** `CloneInfo.endpoints` gives `gateway_url`, `control_url` (a C3 control service
for that clone: `retry_cap`, `shed`, `db_failover`, `canary_weight` with TTLs) and `stats_urls` (`/stats` per
service). So the production telemetry adapter (C1) and lever adapter (C3) work unchanged against a clone; the
brain's judge can score a clone experiment exactly as it scores a production one. Owner 2 tags clone telemetry
with `clone_id`.

**Lab primitives** (`LAB_CATALOG`, also `fixtures/lab_catalog.json`; clone-only, never production). Every apply
carries `ttl_s` (≤ `max_ttl_s`) and the clone reverts it on its own when it expires. Wording deliberately avoids
world vocabulary, because investigators read this catalog (`tests/test_boundary.py` checks it).

| Action | Params | What it does physically | Undo |
| --- | --- | --- | --- |
| `retry_policy` | `max_retries 0–5`, `timeout_ms 50–10000` | Orders' call policy towards Payments | back to the clone's `CloneSpec.retry_policy` |
| `db_latency` | `extra_ms 0–5000` | every primary-DB query costs `extra_ms` more, for `ttl_s` (a transient slowdown) | extra cost removed |
| `db_capacity` | `capacity_qps > 0` | primary DB capacity for the app limited to `capacity_qps` (what a runaway batch job does) | job paused |
| `cpu_limit` | `service`, `cpus 0.05–8` | `docker update --cpus` on that service | original limit |
| `service_kill` | `service` | stop the container for `ttl_s`, then start it | start it |
| `service_restart` | `service` | restart once (`reversible=False`, one-shot) | — |

```python
from faultline_contracts import HttpCloneLab, CloneSpec, WorkloadSpec

lab = HttpCloneLab("http://localhost:9910")                    # create/reset block until healthy (≈ 20–40 s)
c = lab.create(CloneSpec(name="h_meta", workload=WorkloadSpec(rps=80)))
h = lab.apply(c.clone_id, "db_latency", {"extra_ms": 800}, ttl_s=20)   # reproduce: transient DB slowdown
# ... wait, read c.endpoints.stats_urls with the C1 adapter, then C3 levers via c.endpoints.control_url ...
lab.undo(h); lab.reset(c.clone_id); lab.destroy(c.clone_id)
```

### Clone manager HTTP API (`http://localhost:9910`) — what `HttpCloneLab` calls

| Request | Body → Returns | Notes |
| --- | --- | --- |
| `GET /lab/catalog` | → `list[LabActionSpec]` | |
| `GET /clones` · `GET /clones/{id}` | → `list[CloneInfo]` · `CloneInfo` | |
| `POST /clones` | `CloneSpec` → `CloneInfo` | blocks until `ready`; `409` when `MAX_CLONES` (3) are alive |
| `POST /clones/{id}/reset` | → `CloneInfo` | clears every lab action and lever, drains, verifies a healthy window; `503` if it can't |
| `DELETE /clones/{id}` | → `CloneInfo` (`destroyed`) | idempotent |
| `POST /clones/{id}/workload` | `WorkloadSpec` → `CloneInfo` | change the replayed request rate |
| `POST /clones/{id}/actions` | `LabActionRequest {action, params, ttl_s}` → `LabActionHandle` | `400` bad params / ttl; `409` clone not `ready` |
| `DELETE /clones/{id}/actions/{action_id}` | → `LabActionHandle` (`undone`) | idempotent |
| `GET /clones/{id}/actions` | → `list[LabActionHandle]` | active and recently expired |
| `GET /healthz` | → `200` | |

Errors are `{"detail": "..."}`; the client maps all 4xx to `LabError`. `LabActionHandle.status` reuses C3's
`ActionStatus` (`active | undone | expired | failed`).

**Rules.** (1) C6 endpoints only reach clones: the manager refuses anything that would touch the production
Compose project. (2) Nothing in `clone.py` imports C5, and `:9900` is not routable from a clone's network.
(3) Budget: production + 2 investigation clones; 3 only after profiling on the demo machine (`MAX_CLONES`).
(4) Reproduction recipes are documented by Owner 1 in `sandbox/INTEGRATION.md` (e.g. `db_latency 800 ms / 20 s`,
`db_capacity 40`, `cpu_limit payments 0.1`); investigators decide when and why to use them.

## Fairness rules

1. Nothing under `faultline/` imports `faultline_contracts.fault` (enforced by `tests/test_boundary.py`).
   `faultline_contracts/__init__.py` does not re-export it.
2. Fault-controller state, world labels and trigger timing never appear in telemetry, fingerprints, logs,
   or audit events. Fingerprint fixtures are checked for the strings `world`, `storm`, `degraded`, `fault`,
   `cpu_starve` — sandbox log messages should avoid them too.
3. Faultline reaches the sandbox only through C1 (telemetry/ES) and C3 (control service on :9901).
4. On the OTel Demo: filter flagd attributes out of telemetry and never use flag flips as levers.

## Fakes and examples

Built for parallel development (in `src/faultline_contracts/fakes/`):

- `FakeWorld` — a small simulator of the hero (storm / degraded DB / CPU-starve) producing fingerprints.
- `FakeLeverAdapter` — in-memory `LeverAdapter` with ttl expiry and param validation against `CATALOG`.
- `ReplayTelemetrySource` — a `TelemetrySource` over a list of fingerprints (e.g. the series fixtures).
- `examples/run_hero.py` — end-to-end hero run against the fakes.

Faultline code (and tests) may use `FakeLeverAdapter` / `ReplayTelemetrySource`; `FakeWorld` is the hidden
truth and belongs to bench/tests only.

## Fixtures (`fixtures/`, regenerate with `uv run python scripts/make_fixtures.py`)

Deterministic; timeline starts 2026-09-19T15:00:00Z, fault at 15:01:00, experiment 15:02:00–15:02:20.

| File | Model | Content |
| --- | --- | --- |
| `fingerprint_healthy.json` | `Fingerprint` | 80 checkouts/s, ~80 db qps, retry_ratio 1.0, query p50 ~15 ms |
| `fingerprint_storm.json` | `Fingerprint` | World A incident: ~320 db qps, retry_ratio ~4, query p50 > 500 ms, pool 1.0 |
| `fingerprint_degraded_db.json` | `Fingerprint` | World B incident: same as above up to noise |
| `series_storm_experiment.json` | `list[Fingerprint]` | 60 s healthy, 60 s incident, 20 s retry cap 0 (recovers), 30 s after release (stays healthy) |
| `series_degraded_db_experiment.json` | `list[Fingerprint]` | same timeline; during cap db qps ~80 but query time stays high; incident returns after release |
| `triage_hero.json` | `TriageResult` | ambiguous; H_meta vs H_db; predictions for `retry_cap_0_20s` and `db_failover_30s` |
| `catalog.json` | `list[LeverSpec]` | `CATALOG` |
| `experiments.json` | `list[Experiment]` | candidate experiments |
| `verdict_storm.json` | `Verdict` | H_meta confirmed, with observations |
| `audit_hero.jsonl` | `AuditEvent` per line | one incident's full trail |
| `fault_state_storm.json` | `FaultState` (C5) | storm injected at 15:01:00 |
| `lab_catalog.json` | `list[LabActionSpec]` (C6) | `LAB_CATALOG` |
| `clone_hero.json` | `CloneInfo` (C6) | investigator A's ready clone with an active `db_latency` action |

JSON Schema for the main models is in `schema/` (regenerate with `uv run python scripts/export_schemas.py`).

## Tests

```bash
uv run pytest -q     # fixtures valid + round-trip, problems() checks, strict OpenAI schema, schema freshness, fairness boundary
```
