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
- Live baseline hardening: incident noise uses the steady 30-second breached
  tail, and the judge refuses an after-release confirmation with no true healthy
  baseline windows.

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

## Elastic Agent Builder: read-only Investigation agent

`faultline_brain.elastic_investigation` defines a deliberately non-C2 agent:
**Faultline Investigation** (`faultline-investigation`). Its sole purpose is
to turn bounded, observable evidence into a human explanation. It never
proposes or confirms a diagnosis, selects an experiment, or calls a control
surface; Brain C2 and the noise-model judge retain those responsibilities.

Owner 2 must provide exactly these custom read tools (and no generic index
search tool) before deployment:

- `faultline.incident_timeline`
- `faultline.clone_vs_production`
- `faultline.similar_incidents`
- `faultline.incident_context`

Each must be a parameterized, read-only query over only its intended evidence
indices. Do not expose `faultline-audit` records containing action controls,
C5/controller documents, hidden world labels, fault-trigger timestamps, or
benchmark metadata.

With a deployment management key, run:

```bash
PYTHONPATH=contracts/src:faultline/brain/src \
FAULTLINE_ELASTICSEARCH_URL=https://... KIBANA_URL=https://... \
ELASTIC_AGENT_BUILDER_API_KEY=... OPENAI_API_KEY=... OPENAI_MODEL=... \
python faultline/brain/scripts/deploy_elastic_investigation_agent.py
```

The script creates the `faultline-openai-investigation` OpenAI
`chat_completion` inference endpoint, verifies the four tool IDs, then creates
or updates the agent. `--dry-run` validates the configuration and both hero
fixtures without credentials. `ELASTIC_AGENT_BUILDER_API_KEY` needs
`manage_inference` plus Agent Builder management privileges; it is distinct
from the telemetry writer key. Deployment credentials are intentionally not
committed to this repository.

See `DEVPOST_NOTES.md` for evidence-bounded OpenAI, Token Company, and Codex
write-up copy.
