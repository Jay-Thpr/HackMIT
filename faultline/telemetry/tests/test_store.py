from datetime import datetime, timedelta, timezone

from faultline_contracts import Fingerprint

from faultline_telemetry.store import ElasticsearchFingerprintStore


def matches(document, query):
    if "bool" in query:
        clauses = query["bool"]
        return (
            all(matches(document, item) for item in clauses.get("filter", []))
            and not any(matches(document, item) for item in clauses.get("must_not", []))
            and sum(matches(document, item) for item in clauses.get("should", []))
            >= clauses.get("minimum_should_match", 0)
        )
    if "term" in query:
        return all(document.get(key) == value for key, value in query["term"].items())
    if "exists" in query:
        return document.get(query["exists"]["field"]) is not None
    if "range" in query:
        bounds = query["range"]["window_start"]
        return bounds["gte"] <= document["window_start"] < bounds["lt"]
    return True


class FakeElastic:
    def __init__(self): self.docs = []
    def index(self, *, index, document): self.docs.append((index, document))
    def search(self, *, index, query, sort, size=10000):
        self.last_query = query
        hits = [{"_source": doc} for name, doc in self.docs if name == index and matches(doc, query)]
        return {"hits": {"hits": hits}}


def test_store_round_trips_a_c1_window():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fp = Fingerprint(window_start=start, window_end=start + timedelta(seconds=5))
    store = ElasticsearchFingerprintStore(FakeElastic())
    store.write(fp)
    assert store.window(fp.window_start, fp.window_end) == fp
    assert store.series(start, start + timedelta(seconds=5)) == [fp]


def test_store_keeps_clone_identity_outside_the_c1_fingerprint():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fp = Fingerprint(window_start=start, window_end=start + timedelta(seconds=5))
    client = FakeElastic()
    store = ElasticsearchFingerprintStore(client)

    store.write(fp, incident_id="inc-7", clone_id="clone-9")

    assert client.docs[0][1]["clone_id"] == "clone-9"
    assert client.docs[0][1]["environment"] == "clone"
    assert store.query(start, start + timedelta(seconds=5), incident_id="inc-7", clone_id="clone-9") == [fp]


def test_default_reads_isolate_production_from_clone_and_unknown_environments():
    from faultline_contracts import ServiceStats

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=5)
    production = Fingerprint(window_start=start, window_end=end, services={"orders": ServiceStats(qps=100)})
    clone = production.model_copy(update={"services": {"orders": ServiceStats(qps=3)}})
    client = FakeElastic()
    store = ElasticsearchFingerprintStore(client)
    store.write(production, incident_id="inc")
    store.write(clone, incident_id="inc", clone_id="clone-1")
    store.write(clone, incident_id="inc", clone_id="clone-2")
    client.index(index="faultline-fingerprints", document={**clone.model_dump(mode="json"), "environment": "fixture"})
    client.index(index="faultline-fingerprints", document={**clone.model_dump(mode="json"), "environment": "clone"})
    client.index(index="faultline-fingerprints", document={**clone.model_dump(mode="json"), "environment": "production", "clone_id": "malformed"})

    assert store.window(start, end) == production
    assert store.series(start, end) == [production]
    assert store.query(start, end, incident_id="inc") == [production]
    assert store.query(start, end, incident_id="inc", clone_id="clone-1") == [clone]


def test_legacy_unscoped_documents_are_production_only_without_clone_identity():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=5)
    fp = Fingerprint(window_start=start, window_end=end)
    client = FakeElastic()
    document = fp.model_dump(mode="json")
    client.index(index="faultline-fingerprints", document=document)
    client.index(index="faultline-fingerprints", document={**document, "clone_id": "legacy-clone"})
    store = ElasticsearchFingerprintStore(client)
    assert store.window(start, end) == fp
    assert store.query(start, end, clone_id="legacy-clone") == [fp]
