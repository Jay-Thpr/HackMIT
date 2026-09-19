"""End-to-end smoke test against a real Elasticsearch.

    FAULTLINE_ELASTICSEARCH_URL=http://localhost:9200 uv run python scripts/es_smoke.py

Reads FAULTLINE_ELASTICSEARCH_URL / FAULTLINE_ELASTICSEARCH_API_KEY from the
environment (falling back to a repo-root .env), ensures index templates, writes
three C1 fingerprints and two C4 audit events under a fresh incident id, reads
them back through the store and sink, and prints the ES|QL incident timeline.
"""

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from faultline_contracts import Fingerprint, ServiceStats
from faultline_contracts.audit import Actor, AuditEvent, EventKind, Stage
from faultline_contracts.fingerprint import DbStats, SloStatus

from faultline_telemetry import (
    ElasticsearchAuditSink,
    ElasticsearchFingerprintStore,
    HttpElasticsearchClient,
    ensure_index_templates,
    incident_timeline,
    load_repo_dotenv,
)


def main() -> int:
    load_repo_dotenv(Path(__file__))
    url = os.environ.get("FAULTLINE_ELASTICSEARCH_URL")
    if not url:
        print("FAULTLINE_ELASTICSEARCH_URL is not set", file=sys.stderr)
        return 2
    client = HttpElasticsearchClient(url, api_key=os.environ.get("FAULTLINE_ELASTICSEARCH_API_KEY"))

    ensure_index_templates(client)

    incident_id = "smoke-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
    store = ElasticsearchFingerprintStore(client)
    for i in range(3):
        window_start = start + timedelta(seconds=5 * i)
        store.write(
            Fingerprint(
                window_start=window_start,
                window_end=window_start + timedelta(seconds=5),
                services={"orders": ServiceStats(qps=80.0 + i, retry_ratio=1.0 + 0.1 * i)},
                db=DbStats(qps=85.0, query_p99_ms=40.0 + i),
                slos=[SloStatus(name="checkout", metric="svc.gateway.p99_ms", threshold=1000.0, value=120.0 + i, breached=False)],
            ),
            incident_id=incident_id,
        )
    sink = ElasticsearchAuditSink(client)
    for i, kind in enumerate((EventKind.detect, EventKind.triage)):
        sink.write(
            AuditEvent(
                incident_id=incident_id,
                ts=start + timedelta(seconds=i),
                stage=Stage.detect if i == 0 else Stage.triage,
                kind=kind,
                actor=Actor.math,
                summary=f"smoke event {i}",
            )
        )

    client.refresh("faultline-fingerprints")
    client.refresh("faultline-audit")

    found = store.query(start, start + timedelta(seconds=15), incident_id=incident_id)
    assert len(found) == 3, f"expected 3 fingerprints, got {len(found)}"
    events = sink.query(incident_id)
    assert len(events) == 2, f"expected 2 audit events, got {len(events)}"

    rows = incident_timeline(client, incident_id, start, start + timedelta(seconds=15))
    assert len(rows) == 3, f"expected 3 timeline rows, got {len(rows)}"
    for row in rows:
        print(row)
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
