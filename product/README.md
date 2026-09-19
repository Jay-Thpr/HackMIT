# Faultline Product CLI

This is the Product-lane vertical slice from the project plan: a Warp-friendly CLI,
C3 orchestration, C4 audit trail, a mock Devin boundary, and audit-backed reporting.

```bash
uv run faultline watch --fixture storm --incident demo-storm-001
uv run faultline report --incident demo-storm-001
```

For live sandbox telemetry and control:

```bash
cd sandbox && docker compose up -d --build
# trigger a storm from the bench side (Owner 3 / demo script), then:
cd ../product && uv run faultline watch --telemetry sandbox --levers sandbox --incident live-1
```

Runtime orchestration depends on the shared `LeverAdapter`, `TelemetrySource`, and
`AuditSink` contracts. Owner 2's live telemetry/Elasticsearch sink and Owner 4's
TTL-backed sandbox adapter can replace the fixture implementations without changing
the orchestrator or CLI flow. The Product code does not import C5 or hidden sandbox
state.
