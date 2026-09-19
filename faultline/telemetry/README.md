# Track 2 — Telemetry + Elastic

This package owns C1 production telemetry and the C4 Elasticsearch audit sink.

Stage 1 provides configuration, dependency ports, and a tested audit sink. The
next stage will add snapshot polling/OTel ingestion and C1 fingerprint assembly.

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
| `fingerprint.py` | Stage 2 C1 five-second fingerprint assembly |
