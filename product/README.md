# Faultline Product CLI

This is the Product-lane vertical slice from the project plan: a Warp-friendly CLI,
C3 orchestration, C4 audit trail, a mock Devin boundary, and audit-backed reporting.

```bash
uv run faultline watch --fixture storm --incident demo-storm-001
uv run faultline report --incident demo-storm-001
```

Use the real Brain planner and math judge without an API key:

```bash
uv run faultline watch --brain live --incident brain-demo-001
```

For OpenAI-backed triage, install the optional client with `uv sync --extra llm`
and set `OPENAI_API_KEY`.

For live sandbox telemetry and control:

```bash
cd sandbox && docker compose up -d --build
# trigger a storm from the bench side (Owner 3 / demo script), then:
cd ../product
uv run faultline watch \
  --telemetry sandbox \
  --levers sandbox \
  --brain live \
  --lab-url http://127.0.0.1:9910 \
  --incident live-1
```

The patch reference decides what gets built as orders-v2: a Devin PR URL is fetched as
`refs/pull/N/head`, `branch:<name>` (the prebuilt fallback) is fetched from origin, each into a
detached worktree under `.faultline/worktrees/`. `--canary-context <dir>` overrides this with an
operator checkout. With `--devin`, `DEVIN_API_KEY` (a `cog_` service-user key; the Devin API v3 rejects legacy
`apk_` keys) and `DEVIN_ORG_ID`, a Devin session writes the fix (capped at `--devin-acu-limit`
ACUs, default 5); when clone
verification or the production canary fails, the measured evidence is posted back into the same
session and the revised PR is fetched and verified again (`--max-revisions`, default 1). A patch
that cannot be revised pages a human with the evidence.

Add `--lab-url http://127.0.0.1:9910` (clone manager: `cd sandbox && uv run uvicorn
services.lab.app:app --port 9910`) and, before the production canary, Faultline builds the
patch into a clean C6 clone, routes all clone traffic to the patched orders-v2, replays the
reproduction recipe for the diagnosis (`H_meta`: 800 ms DB latency for 20 s; `H_db`: DB capacity
40 qps) and requires the clone's checkout SLO to recover on its own. A patch that fails is
refused with the measured evidence and a human is paged; without a lab the step is `skipped`
and the v5 canary path runs unchanged. Production is never touched; the clone is destroyed.

Sandbox telemetry and levers are an atomic live profile. Live mode automatically selects
the measured Brain planner/judge and refuses fixture evidence. The canary checkout is built
as orders-v2 before traffic shifts; if it cannot be prepared or measured, the run is reported
as escalated rather than successful.

Live `/stats` → C1 fingerprints are built by Owner 2's canonical
`faultline_telemetry.fingerprint.fingerprint_from_stats` (missing data stays `None`, fixed
window boundaries); Product only adds the polling loop, breach wait, and the optional
`orders_v2` canary service on top, so the judge's baselines match what lands in Elasticsearch.

Runtime orchestration depends on the shared `LeverAdapter`, `TelemetrySource`, and
`AuditSink` contracts. Owner 2's live telemetry/Elasticsearch sink and Owner 4's
TTL-backed sandbox adapter can replace the fixture implementations without changing
the orchestrator or CLI flow. The Product code does not import C5 or hidden sandbox
state.
