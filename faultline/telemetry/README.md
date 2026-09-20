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
| `tokens.py` | OpenAI-token measurement: raw public snapshots versus compressed C1 evidence per incident |

## Elastic Cloud

Set `FAULTLINE_ELASTICSEARCH_URL` (deployment endpoint) and
`FAULTLINE_ELASTICSEARCH_API_KEY` (Kibana → Stack Management → API keys; needs
write on `faultline-*` and read on `_query`) in the repo-root `.env` — see
`.env.example`. Both the product CLI and `scripts/es_smoke.py` load it via
`load_repo_dotenv` without overriding real environment variables. Leave the URL
unset to skip Elasticsearch persistence entirely. For local development, point
the URL at `http://localhost:9200` with no API key.

## Read-only Elastic diagnostics (`scripts/es_doctor.py`)

```bash
uv run python scripts/es_doctor.py [--incident-id ID] [--require-mirror]
```

Preflights the primary (`FAULTLINE_ELASTICSEARCH_*`) and display-mirror
(`FAULTLINE_OBSERVABILITY_ELASTICSEARCH_*`, falling back to
`FAULTLINE_ELASTICSEARCH_MIRROR_*`) projects with bounded read-only requests
only: `GET /_index_template/{index}` and `size: 1` `POST /{index}/_search` for
the latest C1 `window_end` (production-filtered) and C4 `ts`. It never indexes,
refreshes, PUTs templates, touches Kibana/Agent Builder, or constructs the
mirror worker — no outbox is created.

Output is sanitized JSON (no URLs, keys, or incident IDs) plus an exit code:
`0` all requested checks `present`, `1` some check needs attention (denied,
missing, empty, partial, malformed, future timestamp), `2` required config
absent/incomplete/invalid. A configured-but-broken mirror fails the run; an
absent mirror fails only under `--require-mirror`. Timestamp age is reported as
an observation, never as delivery lag, and template presence alone does not
prove ingestion works.

Note: the template `GET` may require privileges the runtime API key lacks — a
`forbidden` template check means the key can't see templates, not that
ingestion is broken. Only runtime `*_API_KEY` values are used; never escalate
to `*_SETUP_API_KEY`.
