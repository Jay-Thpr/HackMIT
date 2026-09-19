# C2 / Brain Handoff

## What is implemented

- Telemetry fairness reader and canonical metric access.
- Noise model: per-metric baseline with `sigma = max(std, 10% of typical)`.
- Math judge: observations, normalized support, and `confirms_if`-gated verdicts.
- Strict OpenAI triage wiring with semantic validation retry and token-usage callback.
- Experiment planner: directional separation minus blast-radius cost.
- Deterministic active benchmark runner.

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

- Passive-only, LLM-only, nearest-centroid, and random-lever benchmark baselines.
- Aggregate benchmark reporting.
- Product integration must supply the C2 judge with a complete telemetry series, C4-derived experiment windows, and separate healthy/incident baselines.
