# C2 / Brain Handoff

## What is implemented

- Telemetry fairness reader and canonical metric access.
- Noise model: per-metric baseline with `sigma = max(std, 10% of typical)`.
- Math judge: observations, normalized support, and `confirms_if`-gated verdicts.
- Strict OpenAI triage wiring with semantic validation retry and token-usage callback.
- Experiment planner: directional separation minus blast-radius cost.
- Deterministic active benchmark runner.
- Passive-only, LLM-only, nearest-centroid, and random-lever benchmark baselines.
- Frozen-suite orchestration with JSON-ready aggregate reporting.
- C6 clone-investigator evidence loop: reproduction similarity, recovery,
  measured C2 prediction scoring, and guaranteed lab-action/clone cleanup.
- Production confirmation follow-up: an unconfirmed diagnostic probe can select
  the gentlest untried positive confirmation experiment for its leading cause.

## Interfaces C2 consumes

- C1: `Fingerprint` and `TelemetrySource` five-second windows.
- C3: candidate `Experiment`s and lever blast radius.
- C4: audit events; `experiment_windows()` is the sole phase-boundary source.

## Telemetry requirements for Track 2

- Emit `db.query_p50_ms` / `db.query_p99_ms` from app-observed DB wait time, including pool wait.
- Emit `db.qps` from queries issued, not completed-query throughput.
- Omit absent telemetry entirely; do not substitute zero values. The sandbox has no `fraud_check` service.
- Never expose C5 state, trigger timing, or Envoy fault-filter metrics in a fingerprint.

## Remaining Brain work

- Run the frozen suite against live sandbox fingerprints and publish only that
  measured output as benchmark evidence. Fixture results test mechanics, not
  live accuracy.
- Capture OpenAI `usage_sink` output for a compressed-fingerprint versus
  raw-telemetry token comparison before making a Token Company claim.
- Wire `CloneInvestigator` to the live C6 manager and clone C1/C3 adapters for
  two concurrent hypothesis investigations; the evidence math is implemented,
  but it has not yet been exercised against Docker clones.
- Product integration must supply the C2 judge with a complete telemetry series,
  C4-derived experiment windows, and separate healthy/incident baselines.

See `DEVPOST_NOTES.md` for evidence-bounded OpenAI, Token Company, and Codex
write-up copy.
