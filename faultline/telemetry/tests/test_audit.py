from datetime import datetime, timezone

from faultline_contracts.audit import Actor, AuditEvent, EventKind, Stage

from faultline_telemetry.audit import ElasticsearchAuditSink


class FakeElastic:
    def __init__(self):
        self.documents = []
        self.last_search = None

    def index(self, *, index, document):
        self.documents.append((index, document))

    def search(self, *, index, query, sort):
        self.last_search = (index, query, sort)
        incident_id = query["term"]["incident_id.keyword"]
        docs = [doc for _, doc in self.documents if doc["incident_id"] == incident_id]
        return {"hits": {"hits": [{"_source": doc} for doc in docs]}}


def event(incident_id="inc-1"):
    return AuditEvent(
        event_id="event-1",
        incident_id=incident_id,
        ts=datetime(2026, 9, 19, tzinfo=timezone.utc),
        stage=Stage.detect,
        kind=EventKind.detect,
        actor=Actor.math,
        summary="checkout breached",
    )


def test_write_serializes_a_c4_event_to_the_audit_index():
    client = FakeElastic()
    sink = ElasticsearchAuditSink(client)

    sink.write(event())

    assert client.documents[0][0] == "faultline-audit"
    assert client.documents[0][1]["incident_id"] == "inc-1"
    assert client.documents[0][1]["schema_version"] == "1"


def test_query_filters_and_sorts_by_incident():
    client = FakeElastic()
    sink = ElasticsearchAuditSink(client)
    sink.write(event("inc-1"))
    sink.write(event("inc-2"))

    found = sink.query("inc-1")

    assert [item.incident_id for item in found] == ["inc-1"]
    assert client.last_search[2] == [{"ts": "asc"}, {"event_id": "asc"}]
