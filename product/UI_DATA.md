# UI data contract — what the orchestrator emits, and where each panel reads it

For whoever builds the UI. Everything below is produced by `faultline watch` today and was
taken from real runs (`live-11`, `live-13` in `/tmp/faultline-live/audit.jsonl`). Nothing here
requires a product change; if you need a field that isn't listed, ask Owner 4 rather than
parsing `summary` strings.

## Sources

| Source | Where | Use |
|---|---|---|
| C4 audit events | `product/state/faultline-audit.jsonl` (or `--audit-log <path>`), one JSON object per line; also ES index `faultline-audit` when `--elasticsearch-url` is set | Every panel except the chart |
| C1 fingerprints | ES index `faultline-fingerprints` (Owner 2's `ElasticsearchFingerprintStore`), documents carry `incident_id` and, for clones, `clone_id`; or poll the sandbox `/stats` directly (`:8101` orders, `:8102` payments, `:8103` loadgen, `:8104` orders-v2) and build with `faultline_telemetry.fingerprint_from_stats` | The chart |
| Owner 2 analytics | `faultline_telemetry.analytics.ElasticsearchTelemetryAnalytics`: `ui_data`, `similar_incidents`, `clone_production_similarity` | Similar-incidents row, clone-vs-production similarity |

Audit event envelope (C4, `faultline_contracts.AuditEvent`):

```json
{"schema_version":"1","event_id":"…","incident_id":"live-13","ts":"2026-09-19T23:57:12Z",
 "stage":2,"kind":"detect","actor":"orchestrator","summary":"checkout SLO breached",
 "action_id":null,"experiment_id":null,"payload":{…}}
```

`stage` 1–8 = ingest, detect, triage, experiment, mitigate, patch, canary, report.
`actor` is the split the PRD asks the UI to show: **`llm` = reasoning panel, `math` = measured-evidence
panel**, `adapter` = actions, `orchestrator` = control flow. Don't rely on order within one `ts`.

## Panel → events

### Timeline / status strip
All events for the incident, in `ts` order. `stage` gives the pipeline position; the last
`report` event's `payload.diagnosis`, `canary_status`, `clone_verification` give the outcome.

### LLM reasoning panel (`actor == "llm"`)
`stage 3, kind triage`: `payload.hypotheses` (ids, e.g. `["H_meta","H_db","H_cpu"]`), `payload.ambiguous`.
Hypothesis labels/descriptions and the prediction matrix are in the `TriageResult` (C2); today the
audit carries only ids. If you want labels/predictions/`confirms_if` rendered, tell Owner 4 and the
triage event will carry the `TriageResult` dump — it is a five-line change.

### Measured-evidence panel (`actor == "math"`)
- `stage 5, kind verdict`: `payload.diagnosis`, `payload.confirmed`, `payload.observations[]`, each
  `{experiment_id, metric, phase: "during"|"after_release", baseline, measured, sigma, z, direction}`.
  This is the table the PRD wants next to the LLM panel (z-scores against noise). Observations are
  duplicated per hypothesis; de-dup on `(metric, phase)` for display.
- `stage 4, kind triage, payload.investigation == true` (one per hypothesis): `hypothesis_id`,
  `clone_id`, `recipe`, `reproduced`, `recovered`, `prediction_matches`/`prediction_total`,
  `survives`, `evidence.{reproduction,recovery}.{shared_metrics,matching_metrics,mean_abs_z}`.
  These are the **investigator panels** ("two clones appear"). `survives=false` with
  `reproduced=true` is normal today (see judge-calibration notes to Owner 3).
- `stage 6, kind canary_update, actor math`: clone verification of the patch — `payload.status`
  (`passed|failed|skipped`), `clone_id`, `recipe`, `evidence.{incident_reproduced,
  breached_after_settle, windows_after_settle, orders_v2_p99_ms_after, retry_ratio_after}`.

### Planner / experiment
- `[plan] … selected, blast radius N%` is only in the renderer today; the audit has
  `stage 4, kind experiment_start` with `payload.hold_s`, `ttl_s`, `experiment_id`. The planner's
  **candidate table** (separation vs blast radius per experiment) is not yet emitted; if the UI
  wants it (judge-visible item 4), Owner 4 will add a `stage 4` event with
  `payload.candidates[] = {experiment_id, lever_id, separation, score, blast_radius_pct, selected}`.
- Phase boundaries for shading the chart: `kind experiment_start` (cap on) and `kind experiment_end`
  (released) with matching `experiment_id`; `faultline_contracts.experiment_windows(events)` computes
  them for you. During = start→end; after-release = end→+default_watch_s (20 s for retry_cap).

### Actions / audit log (`actor == "adapter"`)
`kind action_apply` / `action_undo` in stages 4, 5, 7: `payload.lever_id`, `params`, `ttl_s`,
`applied_at`, `status`, `action_id`. Every `action_apply` has a matching `action_undo` with the same
`action_id` (or the TTL expired on the target). `stage 7 action_apply` also has `payload.target`
(`patch_reference`, `version`, `source_revision`, `service_name`).

### Patch / Devin
`stage 6, kind patch_opened`: `payload.provider` (`devin|fallback`), `reference` (PR URL or
`branch:…`), `revision` (0, 1, …), `session_id`. A revision after measured failure adds
`payload.evidence` (the text sent back to Devin). Link `reference` and
`https://app.devin.ai/sessions/<session_id>`.

### Canary
`stage 7, kind canary_update, actor orchestrator`: `payload.v2_weight`, `target`. Failure path:
`stage 7, kind refused` (`payload.reason`) + `kind page_human`.

### Human paging
`kind page_human` (any stage) — show prominently; `kind refused` right before it says why.

## The chart

DB query latency and request load over time, with the experiment shaded. Series from ES
(`faultline-fingerprints`, filter `incident_id`, optionally `clone_id` for a clone's own chart) or
from `/stats` polling every 5 s. Metric keys (C1 canonical): `db.query_p50_ms`, `db.query_p99_ms`,
`db.qps`, `svc.orders.retry_ratio`, `svc.gateway.p99_ms`, `svc.gateway.error_rate`,
`db.pool_busy_ratio`; SLO threshold is `slos[0].threshold` (1000 ms on `svc.gateway.p99_ms`).
Reference magnitudes: healthy p50 ≈ 50 ms / 80 qps / retry 1.0; storm ≈ 1100 ms / 320 qps / 4.0.
Missing values are `null`, never 0 — draw a gap, not a drop.

## Live run for UI development

```bash
cd sandbox && docker compose up -d && uv run uvicorn services.lab.app:app --port 9910   # stack + lab
cd product && source ~/.config/faultline/env && uv run faultline watch \
  --telemetry sandbox --levers sandbox --brain live --lab-url http://127.0.0.1:9910 \
  --elasticsearch-url "$FAULTLINE_ELASTICSEARCH_URL" --incident ui-dev-1
# ~2 min later, from integration/: inject the storm via C5 (see integration/live_loop.py)
```

The audit file is appended live; `/tmp/faultline-live/audit.jsonl` already holds thirteen real
incidents (`live-1`…`live-13`) to develop against without a running stack.
