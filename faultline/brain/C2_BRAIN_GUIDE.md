# C2 / Brain — study guide (Owner 3)

Everything here is grounded in the frozen contract (`contracts/src/faultline_contracts/`). If code and this doc disagree, the contract wins.

---

## 0. The one-liner

**LLM proposes and explains; your math decides.** The Brain turns telemetry into a *defensible* diagnosis: the LLM guesses causes and predicts what each experiment will do; your measurement rules whether the guess beat the noise. That split is the product's whole credibility.

You own two dirs:
- `faultline/brain/` — triage (LLM), noise model, planner, judge
- `bench/` — benchmark runner + baselines

Contract you produce/consume: **C2** = `triage.py` + `openai_schema.py`.

---

## 1. Big-picture flow

```
Owner2 Telemetry          YOU = Brain (Owner3)              Owner4 Product
────────────────          ────────────────────             ──────────────
Fingerprint (C1) ──window/series──▶ Triage(LLM) ─▶ TriageResult(C2) ─▶ Orchestrator + UI
 via TelemetrySource                   │                                    │
                                  Planner(math) ◀─ catalog/Experiment(C3) ──┘  (Owner4 owns levers)
                                       │ picks experiment
                                       ▼
                        Orchestrator APPLIES it via LeverAdapter(C3), writes AuditEvents(C4)
                                       │
      Fingerprint(C1) ◀── during/after windows ──┐
                                       ▼          │ phase boundaries from
                                   Judge(math) ◀──┘ experiment_windows(AuditEvent) (C4)
                                       │
                                       ▼
                                  Verdict(C2) ─▶ Orchestrator (mitigate/patch) + UI (evidence panel)
```

8-stage pipeline (PRD): 1 Ingest → 2 Detect → **3 Triage(you)** → **4 Experiment: plan+judge(you)** → 5 Mitigate → 6 Patch → 7 Canary → 8 Report. Obvious incidents skip 4.

---

## 2. Who you talk to (contract-exact)

**IN — from Owner 2 (Telemetry), C1:**
- Call `TelemetrySource.window(start, end)` / `.series(start, end, step_s)` → `Fingerprint`.
- Read metrics only via `fp.metrics()` → `dict[str, float]` of canonical keys. Never hardcode service names (must also work on the OTel Demo).
- You *read* telemetry; you never produce it.

**IN — from Owner 4 (Product), C3 + C4:**
- C3 `catalog()` → `list[LeverSpec]`; candidate `Experiment`s = the planner's menu. You *rank and choose*; the orchestrator *applies* (`apply`/`undo`). You never pull a lever yourself.
- C4 `experiment_windows(events)` → `ExperimentWindow(experiment_id, start, release)`. This is how the judge learns the **during / after_release** boundaries — from the audit log, not a timer.

**OUT — you produce (consumed by Orchestrator + UI), C2:**
- `TriageResult` (LLM output + ids)
- `Verdict` (math output)
- UI renders two panels side by side: your `reasoning` (LLM) vs your `observations` (measured).

**NO CONTACT — Owner 1 (Sandbox) + C5 (fault controller):**
- Never import `faultline_contracts.fault`. Only `bench/` touches C5 (to inject faults for benchmarking). Runtime brain stays behind C1/C3/C4.

---

## 3. Piece 1 — Triage (LLM)

**Purpose:** fingerprint → candidate causes + testable predictions. Decides `ambiguous` (run an experiment) vs obvious (skip to mitigate).

**Shape you get back — `TriageDraft` (exact LLM output, strict, no defaults):**
- `ambiguous: bool` — False ⇒ obvious, skip experiments
- `reasoning: str`
- `hypotheses: list[Hypothesis]`
  - `Hypothesis(id, label, description, evidence: list[str])` — e.g. id `"H_meta"`, describes trigger + sustaining cause
- `predictions: list[Prediction]` — one per hypothesis × experiment
  - `Prediction(hypothesis_id, experiment_id, during: list[MetricExpectation], after_release: list[MetricExpectation], confirms_if: Confirmation | null)`
  - `MetricExpectation(metric, direction: up|down|flat)`
  - `Confirmation(phase: during|after_release, metric, expect: within_baseline|up|down|flat)` ← the positive test the diagnosis must pass

**`TriageResult`** = `TriageDraft` + `incident_id`, `created_at`, `schema_version` (your code adds these, not the LLM).

**How to call OpenAI (strict structured output — sponsor requirement):**
```python
from faultline_contracts.openai_schema import triage_response_format
from faultline_contracts import TriageDraft, TriageResult

resp = OpenAI().chat.completions.create(
    model="gpt-4.1",
    messages=[{"role":"system","content":SYSTEM},
              {"role":"user","content":fp.model_dump_json()}],
    response_format=triage_response_format())        # strict json_schema from TriageDraft
draft = TriageDraft.model_validate_json(resp.choices[0].message.content)
errs = draft.problems(known_metrics=set(fp.metrics()),
                      experiment_ids={e.id for e in candidates})
# errs non-empty → re-ask LLM with the list. else:
result = TriageResult(**draft.model_dump(), incident_id=incident_id)
```

**`draft.problems(...)` catches (beyond schema):** unknown/malformed metric keys, predictions for unknown hypotheses/experiments, duplicate hypothesis ids, use of reserved `none_of_the_above`, and an ambiguous hypothesis with no positive confirmation test. `confirms_if: null` is permitted for a diagnostic-only experiment, but not for every prediction of a hypothesis. Empty list = OK. Loop: re-ask with the errors until clean.

**Design notes:**
- Keep the prompt cheap — send the compressed fingerprint, not raw telemetry (Token Company angle). Log `usage` every call.
- The LLM must name the metrics its predictions hinge on; those are what the planner diffs and the judge measures.

---

## 4. Piece 2 — Noise model (math) — `noise.py`

**Purpose:** define "did a metric actually move?" so the judge never trusts a wiggle.

**Formula (contract rule — never hardcode latency thresholds):**
- Record several *healthy* windows.
- Per metric: `baseline = mean`, `sigma = max(measured_std, FLOOR_FRAC · |baseline|)`, `FLOOR_FRAC = 0.10`.
- A change counts only if `|z| ≥ ~3` in the predicted direction, `z = (measured − baseline) / sigma`.

**Your code (`faultline/brain/noise.py`):**
- `NoiseModel.from_windows(fps)` → baseline + sigma per metric (skips `None`)
- `.baseline(m)`, `.sigma(m)`, `.z(m, measured)`, `.is_significant(m, measured, k=3.0)`, `.direction(m, measured, k=3.0)` → `Direction` (flat if `|z|<k`, else up/down by sign)

**Why it matters:** magnitudes differ between sim, fixtures, and your real sandbox. Deriving from baseline means the same code works everywhere. `Missing data is omitted, never 0` exists *because* the judge would otherwise read a fake drop.

---

## 5. Piece 3 — Planner (math)

**Purpose:** pick the cheapest experiment that tells the hypotheses apart.

**Inputs:** the `predictions` (which metrics each hypothesis expects to move, and which way), the noise model (how far a move is, in σ), and the C3 candidate experiments + blast radii.

**Math:**
- `separation` = number of metrics where hypotheses predict **different** directions, weighted by how far each is expected to move in σ units.
- `score = separation − λ · blast_radius_pct` (blast = % of otherwise-successful user requests affected).
- Pick the smallest intervention that clears the noise. `retry_cap` (0% blast) before `shed 50%`.

**Candidate levers (C3 `CATALOG`):**
| lever | params | blast radius |
|---|---|---|
| `retry_cap` | `max_retries 0..3` | 0% |
| `shed` | `fraction 0..1` | `fraction·100` |
| `db_failover` | `{}` | 1% |
| `canary_weight` | `v2_weight 0..1` | `v2_weight·100` |

Candidate `Experiment`s (fixtures): `retry_cap_0_20s`, `shed_10_20s`, `shed_50_20s`, `db_failover_30s`. `Experiment(id, lever_id, params, hold_s, blast_radius_pct)` = apply → hold → undo → watch.

---

## 6. Piece 4 — Judge / Verdict (math)

**Purpose:** measure the experiment's response and rule.

**Steps:**
1. Get phase windows from C4: `experiment_windows(audit_events)` → during (lever applied) and after_release (lever undone).
2. For each prediction's metric, pull the measured value from C1 for that phase, compare to baseline via the noise model → build an `Observation`.
3. Update `support` across hypotheses (uniform prior; agreement between predicted and measured directions).
4. **Confirmation test:** the leader is a real diagnosis *only if it passes its own `confirms_if`*. If none passes → `NONE_OF_THE_ABOVE`, page a human.

**Output — `Verdict`:**
- `diagnosis: str` — a hypothesis id or `NONE_OF_THE_ABOVE`
- `confirmed: bool`
- `support: list[HypothesisSupport]` — `(hypothesis_id, support, confirmed: bool|None)`, support sums to 1
- `observations: list[Observation]` — `(experiment_id, metric, phase, baseline, measured, sigma, z, direction)`
- `summary: str`

**Hero worked example (`fixtures/verdict_storm.json`):** retry cap on → db_query_p50 620→17ms (z≈−9.7), errors 0.86→0.01. After release stays flat (z≈0.4). H_meta confirmed, support 0.93. That "stays healthy after release" IS the confirmation test passing.

**The rule that keeps you honest:** LLM confidence never sets `diagnosis`. Only a passed `confirms_if` does.

---

## 7. Hard rules (bite you if ignored)

- Runtime `faultline/` code **never** imports `faultline_contracts.fault`; runtime never imports `.fakes` (tests may). Enforced by `tests/test_boundary.py`.
- No world/fault labels or trigger timing in anything you read. (Your `assert_no_leak` guard backs this up.)
- Missing data = `None`, never 0.
- Units: `_ms`, `_qps`, ratios 0–1. `retry_ratio` = attempts/request (1.0 = none, 4.0 = 3 retries). UTC. 5 s windows (`WINDOW_S`).
- Models use `extra="forbid"` + `schema_version`. Don't invent fields. Don't depend on audit event order within the same timestamp.
- Never hardcode latency thresholds — derive from the noise model.
- LLM calls use the OpenAI API. Log notable Codex usage for the Devpost write-up.
- Contract changes need a PR approved by the consuming owner + regenerated fixtures/schemas. Default: don't touch `contracts/`.

---

## 8. Build order (fastest path to a demoable Brain)

1. **Judge + noise first.** Reproduce `fixtures/verdict_storm.json` from `fixtures/series_storm_experiment.json`. No LLM, no other owner needed. (Noise model already built in `noise.py`.)
2. **Planner** against `fixtures/catalog.json` + `experiments.json`. (Implemented.)
3. **Triage (OpenAI)** with strict structured output and semantic-validation retry. (Implemented with an injected client; mock it in tests.)
4. **bench/** — active deterministic hero runner is implemented. Still add the passive-only, LLM-only, nearest-centroid, and random baselines before publishing benchmark claims.

Setup:
```bash
cd contracts && uv sync && uv run pytest -q          # 65 green
uv run python examples/run_hero.py                   # watch all 3 worlds respond to retry cap
cd ../faultline/brain && uv run pytest -q            # your 20 green
```

---

## 9. What already exists (your code, on branch `c2-brain`, uncommitted)

- `faultline/brain/telemetry.py` — `read_window`/`read_series` (C1 consumption), `assert_no_leak` fairness guard, `metrics_of`.
- `faultline/brain/noise.py` — `NoiseModel` (Piece 2, done).
- `faultline/brain/judge.py` — audited phase-window measurement and hardened confirmation logic.
- `faultline/brain/triage.py` — strict OpenAI client wiring and semantic-validation retry.
- `faultline/brain/planner.py` — separation-versus-blast-radius ranking.
- `bench/` — deterministic active hero-loop runner.

Still to build: benchmark baselines and aggregate benchmark reporting.

---

## 10. Contract cheat-sheet (canonical metric keys)

- `svc.<service>.{qps, p50_ms, p99_ms, error_rate, retry_ratio, timeout_rate}`
- `db.{qps, query_p50_ms, query_p99_ms, pool_busy_ratio}`
- `edge.<src>.<dst>.{qps, p99_ms, error_rate}`
- `slo.<name>.value`

Hero signals live in `db.query_p50_ms`, `db.qps`, `svc.<orders>.retry_ratio`, `svc.<gateway>.error_rate`.
