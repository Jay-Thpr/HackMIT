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
        filters = query["bool"]["filter"]
        incident_id = filters[0]["term"]["incident_id.keyword"]
        clone_id = next((item["term"]["clone_id.keyword"] for item in filters if "term" in item and "clone_id.keyword" in item["term"]), None)
        docs = [doc for _, doc in self.documents if doc["incident_id"] == incident_id and (clone_id is None or doc.get("clone_id") == clone_id)]
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


def test_clone_history_uses_metadata_and_c4_experiment_boundaries():
    client = FakeElastic()
    sink = ElasticsearchAuditSink(client)
    start = event("inc-1").model_copy(update={"kind": EventKind.experiment_start, "experiment_id": "exp-1"})
    end = event("inc-1").model_copy(update={"event_id": "event-2", "ts": datetime(2026, 9, 19, 0, 1, tzinfo=timezone.utc), "kind": EventKind.experiment_end, "experiment_id": "exp-1"})
    sink.write(start, clone_id="clone-1")
    sink.write(end, clone_id="clone-1")

    history = sink.experiment_history("inc-1", clone_id="clone-1")

    assert history[0].experiment_id == "exp-1"
    assert history[0].release == end.ts
    assert client.documents[0][1]["environment"] == "clone"
