from datetime import datetime, timedelta, timezone

from faultline_contracts import Fingerprint

from faultline_telemetry.store import ElasticsearchFingerprintStore


class FakeElastic:
    def __init__(self): self.docs = []
    def index(self, *, index, document): self.docs.append((index, document))
    def search(self, *, index, query, sort, size=10000):
        filters = query["bool"]["filter"]
        time = filters[0]["range"]["window_start"]
        hits = [{"_source": doc} for name, doc in self.docs if name == index and time["gte"] <= doc["window_start"] < time["lt"]]
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
