from datetime import datetime, timedelta, timezone

import pytest

from faultline_contracts import Fingerprint, ServiceStats

from faultline_telemetry.analytics import ElasticsearchTelemetryAnalytics, FingerprintRecord, fingerprint_similarity
from faultline_telemetry.store import ElasticsearchFingerprintStore


class FakeElastic:
    def __init__(self):
        self.docs = []

    def index(self, *, index, document):
        self.docs.append((index, document))

    def search(self, *, index, query, sort, size=10000):
        filters = query.get("bool", {}).get("filter", [])
        docs = [doc for document_index, doc in self.docs if document_index == index]
        for item in filters:
            if "range" in item:
                bounds = item["range"]["window_start"]
                docs = [doc for doc in docs if bounds["gte"] <= doc["window_start"] < bounds["lt"]]
            elif "term" in item:
                field, value = next(iter(item["term"].items()))
                docs = [doc for doc in docs if doc.get(field.removesuffix(".keyword")) == value]
            elif "exists" in item:
                docs = [doc for doc in docs if item["exists"]["field"] in doc]
        return {"hits": {"hits": [{"_source": doc} for doc in sorted(docs, key=lambda doc: doc["window_start"])]}}


def fingerprint(start, qps, retry_ratio=1.0):
    return Fingerprint(
        window_start=start,
        window_end=start + timedelta(seconds=5),
        services={"orders": ServiceStats(qps=qps, retry_ratio=retry_ratio)},
    )


def test_ui_data_reads_only_requested_incident_and_clone():
    start = datetime(2026, 9, 19, tzinfo=timezone.utc)
    client = FakeElastic()
    store = ElasticsearchFingerprintStore(client)
    prod = fingerprint(start, 100)
    clone = fingerprint(start, 95)
    store.write(prod, incident_id="inc-1")
    store.write(clone, incident_id="inc-1", clone_id="clone-a")
    store.write(fingerprint(start, 10), incident_id="inc-2")
    analytics = ElasticsearchTelemetryAnalytics(client)

    production = analytics.ui_data("inc-1", start, start + timedelta(seconds=5))
    clone_rows = analytics.ui_data("inc-1", start, start + timedelta(seconds=5), clone_id="clone-a")

    assert [row.environment for row in production] == ["production"]
    assert [row.clone_id for row in clone_rows] == ["clone-a"]


def test_similar_incidents_ranks_past_production_fingerprints_only():
    start = datetime(2026, 9, 19, tzinfo=timezone.utc)
    client = FakeElastic()
    store = ElasticsearchFingerprintStore(client)
    seed = fingerprint(start, 100, 2.0)
    store.write(fingerprint(start, 99, 2.0), incident_id="near")
    store.write(fingerprint(start, 10, 1.0), incident_id="far")
    store.write(fingerprint(start, 100, 2.0), incident_id="clone-perfect", clone_id="clone-a")

    matches = ElasticsearchTelemetryAnalytics(client).similar_incidents(seed)

    assert [match.incident_id for match in matches] == ["near", "far"]
    assert matches[0].score > matches[1].score


def test_clone_similarity_is_scale_free_and_requires_clone_metadata():
    start = datetime(2026, 9, 19, tzinfo=timezone.utc)
    production, clone = fingerprint(start, 100, 2.0), fingerprint(start, 90, 1.8)
    score, overlap = fingerprint_similarity(production, clone)
    analytics = ElasticsearchTelemetryAnalytics(FakeElastic())
    result = analytics.clone_production_similarity(production, FingerprintRecord(clone, "inc-1", "clone-a"))

    assert 0 < score < 1
    assert overlap == 2
    assert result.clone_id == "clone-a"
    assert result.compared_metrics == 2
    with pytest.raises(ValueError, match="clone-origin"):
        analytics.clone_production_similarity(production, FingerprintRecord(clone, "inc-1", None))


def test_similarity_does_not_treat_missing_metrics_as_zero():
    start = datetime(2026, 9, 19, tzinfo=timezone.utc)
    score, overlap = fingerprint_similarity(
        fingerprint(start, 100),
        Fingerprint(window_start=start, window_end=start + timedelta(seconds=5)),
    )
    assert (score, overlap) == (0.0, 0)
