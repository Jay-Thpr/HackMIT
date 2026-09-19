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
- C2's frozen `triage_hero.json` currently allows H_db to confirm from a
  retry-cap observation. The live run showed that this can also happen under
  uncontrolled host contention. Updating that fixture to require the direct
  `db_failover` recovery test needs the C2 consumers' contract approval; do
  not present retry-cap-only H_db confirmations as final benchmark evidence.
- Product integration must supply the C2 judge with a complete telemetry series,
  C4-derived experiment windows, and separate healthy/incident baselines.

See `DEVPOST_NOTES.md` for evidence-bounded OpenAI, Token Company, and Codex
write-up copy.
