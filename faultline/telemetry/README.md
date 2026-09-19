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

## Modules

| Module | Responsibility |
| --- | --- |
| `config.py` | Local endpoint and index configuration |
| `ports.py` | Small Elasticsearch and stats-source dependency ports |
| `elasticsearch.py` | `HttpElasticsearchClient`: ApiKey auth, explicit search `size`, index templates, `_refresh`, ES|QL `_query` |
| `indices.py` | `ensure_index_templates`: keyword/date mappings plus double coercion for `faultline-fingerprints*` and `faultline-audit*` |
| `esql.py` | `incident_timeline`: ES|QL per-window incident metric summary |
| `dotenv.py` | `load_repo_dotenv`: repo-root `.env` loading without overriding real env vars |
| `audit.py` | C4 `AuditSink` implementation backed by `faultline-audit` |
| `fingerprint.py` | C1 five-second fingerprint assembly from public stats deltas |
| `source.py` | Polling `TelemetrySource`, with optional ES persistence |
| `store.py` | Elasticsearch C1 fingerprint window/series reads |
| `ambiguity.py` | Label-free canonical metric exports for passive ambiguity checks |

## Elastic Cloud

Set `FAULTLINE_ELASTICSEARCH_URL` (deployment endpoint) and
`FAULTLINE_ELASTICSEARCH_API_KEY` (Kibana → Stack Management → API keys; needs
write on `faultline-*` and read on `_query`) in the repo-root `.env` — see
`.env.example`. Both the product CLI and `scripts/es_smoke.py` load it via
`load_repo_dotenv` without overriding real environment variables. Leave the URL
unset to skip Elasticsearch persistence entirely. For local development, point
the URL at `http://localhost:9200` with no API key.
