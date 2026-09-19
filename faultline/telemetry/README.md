# Track 2 — Telemetry + Elastic

This package owns C1 production telemetry and the C4 Elasticsearch audit sink.

The first implementation slices provide configuration, a C4 Elasticsearch audit
sink, public `/stats` polling, C1 fingerprint assembly, and Elasticsearch C1
window persistence/querying. OTel Collector deployment itself is an Owner 1
Compose/service integration dependency; this adapter is ready to consume its
observable output without reading hidden fault state.

## Non-negotiable telemetry semantics

- Windows are fixed five-second UTC intervals.
- DB latency is app-observed issue-to-result time, including pool wait.
- DB QPS counts issued queries, not completed throughput.
- Missing sources are omitted, never replaced by zero. The sandbox has no
  `fraud_check` service or edges.
- Do not ingest fault-controller data, `io_profile`, `/internal/*`, or Envoy
  `*.fault.*` metrics.

## Planned modules

| Module | Responsibility |
| --- | --- |
| `config.py` | Local endpoint and index configuration |
| `ports.py` | Small Elasticsearch and stats-source dependency ports |
| `audit.py` | C4 `AuditSink` implementation backed by `faultline-audit` |
| `fingerprint.py` | C1 five-second fingerprint assembly from public stats deltas |
| `source.py` | Polling `TelemetrySource`, with optional ES persistence |
| `store.py` | Elasticsearch C1 fingerprint window/series reads |
