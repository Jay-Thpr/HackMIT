from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from faultline_contracts import AuditEvent, Actor, EventKind, Fingerprint, Stage
from faultline_contracts.fingerprint import DbStats, SloStatus
from faultline_telemetry.evidence import ElasticsearchEvidenceReader, WINDOW_LIMIT
from faultline_telemetry.store import production_filter

START = datetime(2026, 9, 20, tzinfo=UTC)
END = START + timedelta(seconds=10)


def window(identity="one", incident="current", start=START, clone=None):
    fp = Fingerprint(window_start=start, window_end=start + timedelta(seconds=5),
                     db=DbStats(qps=80, query_p99_ms=900),
                     slos=[SloStatus(name="checkout", metric="db.query_p99_ms", threshold=100, value=900, breached=True)])
    doc = {**fp.model_dump(mode="json"), "incident_id": incident,
           "environment": "clone" if clone else "production"}
    if clone:
        doc["clone_id"] = clone
    return {"_id": identity, "_source": doc}


class Client:
    def __init__(self, windows=(), events=(), error=None):
        self.windows, self.events, self.error = list(windows), list(events), error
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"hits": {"hits": self.windows if kwargs["index"] == "faultline-fingerprints" else self.events}}


def test_context_is_bounded_primary_only_and_preserves_missing_metrics():
    primary = Client([window()])
    context = ElasticsearchEvidenceReader(SimpleNamespace(primary=primary)).context("current", START, END)
    query = primary.calls[0]
    assert query == {
        "index": "faultline-fingerprints", "size": 121, "sort": [{"window_start": "desc"}],
        "query": {"bool": {"filter": [
            {"term": {"incident_id": "current"}}, production_filter(),
            {"range": {"window_start": {"gte": START.isoformat()}}},
            {"range": {"window_end": {"lte": END.isoformat()}}},
        ]}},
    }
    assert context["status"] == "ok"
    assert context["timeline"]["lag_s"] == 5
    item = context["timeline"]["items"][0]
    assert item["reference"] == "c1:one"
    assert "db.query_p50_ms" not in item["metrics"]
    assert context["audit"]["status"] == "empty"


def test_rejects_wrong_incident_clone_and_future_windows():
    client = Client([window(), window("other", "other"), window("clone", clone="c1"), window("future", start=END)])
    result = ElasticsearchEvidenceReader(client).context("current", START, END)
    assert [w["reference"] for w in result["timeline"]["items"]] == ["c1:one"]
    assert result["timeline"]["rejected"] == 3
    assert result["status"] == "partial"
    clone_result = ElasticsearchEvidenceReader(client).context("current", START, END, clone_id="c1")
    assert [w["reference"] for w in clone_result["timeline"]["items"]] == ["c1:clone"]
    assert client.calls[2]["query"]["bool"]["filter"][1] == {"bool": {"filter": [
        {"term": {"environment": "clone"}}, {"term": {"clone_id": "c1"}},
    ]}}


def test_audit_only_exposes_metadata_not_free_text_or_payload():
    event = AuditEvent(event_id="event", incident_id="current", ts=START, stage=Stage.triage,
                       kind=EventKind.triage, actor=Actor.llm, summary="do not disclose",
                       payload={"hidden": "not observable"})
    doc = {**event.model_dump(mode="json"), "environment": "production"}
    result = ElasticsearchEvidenceReader(Client(events=[{"_id": "event", "_source": doc}])).context("current", START, END)
    row = result["audit"]["items"][0]
    assert row["reference"] == "c4:event"
    assert "summary" not in row and "payload" not in row
    assert "do not disclose" not in str(result) and "not observable" not in str(result)


def test_unavailable_is_not_empty_and_remote_errors_are_redacted():
    result = ElasticsearchEvidenceReader(Client(error=RuntimeError("secret remote body"))).context("current", START, END)
    assert result["status"] == "unavailable"
    assert result["timeline"]["reason"] == "RuntimeError"
    assert "secret" not in str(result)
    empty = ElasticsearchEvidenceReader(Client()).context("current", START, END)
    assert empty["status"] == "empty"
    assert empty["timeline"]["lag_s"] is None


def test_truncation_is_explicit_and_never_claims_complete_history():
    client = Client([window(str(i)) for i in range(WINDOW_LIMIT + 1)])
    result = ElasticsearchEvidenceReader(client).context("current", START, END)
    assert result["timeline"]["truncated"] is True
    assert result["status"] == "partial"
    assert len(result["timeline"]["items"]) == WINDOW_LIMIT


def test_history_is_prior_production_deduplicated_and_not_a_diagnosis():
    older = START - timedelta(seconds=10)
    client = Client([window("a", "previous", older), window("b", "previous", older),
                     window("current", "current", older), window("clone", "previous", older, "c1"),
                     window("future", "future", END)])
    fp = Fingerprint.model_validate({k: v for k, v in window()["_source"].items() if k in Fingerprint.model_fields})
    result = ElasticsearchEvidenceReader(client).similar_incidents(fp, incident_id="current", before=START)
    assert len(result["items"]) == 1
    assert result["items"][0]["incident_id"] == "previous"
    assert result["items"][0]["score"] == 1
    assert result["items"][0]["compared_metrics"] > 0
    assert result["score_kind"] == "relative_metric_similarity_not_diagnosis"
    assert result["rejected"] == 3
    call = client.calls[0]
    assert call["size"] == 201
    assert call["query"]["bool"]["must_not"] == [{"term": {"incident_id": "current"}}]
    assert {"range": {"window_end": {"lte": START.isoformat()}}} in call["query"]["bool"]["filter"]
    assert {"term": {"slos.breached": True}} in call["query"]["bool"]["filter"]


def test_incomplete_shards_are_unavailable():
    client = SimpleNamespace(search=lambda **kw: {"_shards": {"failed": 1}, "hits": {"hits": [window()]}})
    result = ElasticsearchEvidenceReader(client).context("current", START, END)
    assert result["status"] == "unavailable"


@pytest.mark.parametrize("start,end", [(END, START), (START, START), (START, START + timedelta(days=1))])
def test_rejects_unbounded_or_invalid_scope(start, end):
    with pytest.raises(ValueError):
        ElasticsearchEvidenceReader(Client()).context("current", start, end)
