from datetime import datetime, timezone

import pytest

from faultline_telemetry.memory import ElasticsearchIncidentMemory, IncidentMemory


class FakeElastic:
    def __init__(self):
        self.docs = []
        self.last_search = None

    def index(self, *, index, document):
        self.docs.append((index, document))

    def search(self, *, index, query, sort, size=10000):
        self.last_search = (index, query, sort, size)
        return {
            "hits": {
                "hits": [
                    {
                        "_score": 0.9,
                        "_source": {
                            "incident_id": "prior-1",
                            "content": "Retry cap held recovery after a dependency slowdown.",
                            "diagnosis": "retry_storm",
                            "created_at": "2026-09-20T00:00:00+00:00",
                        },
                    }
                ]
            }
        }


def test_memory_write_is_a_curated_document_not_a_fingerprint():
    client = FakeElastic()
    memory = IncidentMemory(
        incident_id="incident-1",
        content="Observed retry amplification. The reversible retry cap restored the SLO.",
        diagnosis="retry_storm",
        created_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )
    ElasticsearchIncidentMemory(client).write(memory)

    index, document = client.docs[0]
    assert index == "faultline-incident-memory"
    assert document == {
        "incident_id": "incident-1",
        "created_at": "2026-09-20T00:00:00+00:00",
        "environment": "production",
        "content": "Observed retry amplification. The reversible retry cap restored the SLO.",
        "diagnosis": "retry_storm",
    }
    assert "metrics" not in document


def test_semantic_search_is_environment_scoped_and_returns_context_only():
    client = FakeElastic()
    results = ElasticsearchIncidentMemory(client).search("retries keep increasing during a slow dependency")

    assert results[0].incident_id == "prior-1"
    assert results[0].score == 0.9
    index, query, sort, size = client.last_search
    assert index == "faultline-incident-memory"
    assert query == {
        "bool": {
            "must": [{"match": {"content": {"query": "retries keep increasing during a slow dependency"}}}],
            "filter": [{"term": {"environment": {"value": "production"}}}],
        }
    }
    assert sort == [{"_score": "desc"}, {"created_at": "desc"}]
    assert size == 5


@pytest.mark.parametrize(
    "memory, message",
    [
        (IncidentMemory(incident_id="", content="x"), "incident_id"),
        (IncidentMemory(incident_id="x", content=""), "content"),
        (IncidentMemory(incident_id="x", content="x", environment="unknown"), "environment"),
        (IncidentMemory(incident_id="x", content="x", environment="clone"), "clone_id"),
    ],
)
def test_memory_rejects_incomplete_or_cross_environment_documents(memory, message):
    with pytest.raises(ValueError, match=message):
        memory.document()


def test_search_rejects_empty_or_unbounded_requests():
    memory = ElasticsearchIncidentMemory(FakeElastic())
    with pytest.raises(ValueError, match="search text"):
        memory.search(" ")
    with pytest.raises(ValueError, match="limit"):
        memory.search("valid", limit=21)
