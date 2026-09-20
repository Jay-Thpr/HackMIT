"""Exercise the managed Jina semantic-text path with one safe, idempotent record.

It writes/overwrites only the ``jina-smoke`` incident-memory document, refreshes
the memory index, then checks that a differently worded query retrieves it.
No C1 window, audit trail, fault controller, or verdict is read or written.
"""

from datetime import datetime, timezone
from pathlib import Path

from faultline_telemetry import (
    ElasticsearchIncidentMemory,
    HttpElasticsearchClient,
    IncidentMemory,
    INCIDENT_MEMORY_INDEX,
    client_from_env,
    ensure_index_templates,
    load_repo_dotenv,
)


def main() -> None:
    load_repo_dotenv(Path(__file__))
    client = client_from_env()
    if client is None:
        raise SystemExit("FAULTLINE_ELASTICSEARCH_URL is required")
    try:
        ensure_index_templates(client)
        memory = ElasticsearchIncidentMemory(client)
        memory.write(
            IncidentMemory(
                incident_id="jina-smoke",
                content="A retry amplification incident was safely tested with a reversible retry cap.",
                diagnosis="smoke_test",
                created_at=datetime.now(timezone.utc),
            )
        )
        client.refresh(INCIDENT_MEMORY_INDEX)
        results = memory.search("requests repeatedly retried until a temporary cap stabilized them", limit=1)
        if not results or results[0].incident_id != "jina-smoke":
            raise SystemExit("managed Jina semantic recall did not return the smoke record")
        print("Jina semantic incident-memory smoke check passed")
    finally:
        client.close()


if __name__ == "__main__":
    main()
