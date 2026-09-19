from datetime import datetime, timedelta, timezone

from faultline_contracts import Fingerprint

from faultline_telemetry.store import ElasticsearchFingerprintStore


class FakeElastic:
    def __init__(self): self.docs = []
    def index(self, *, index, document): self.docs.append((index, document))
    def search(self, *, index, query, sort):
        start = query["range"]["window_start"]["gte"]
        end = query["range"]["window_start"]["lt"]
        hits = [{"_source": doc} for name, doc in self.docs if name == index and start <= doc["window_start"] < end]
        return {"hits": {"hits": hits}}


def test_store_round_trips_a_c1_window():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fp = Fingerprint(window_start=start, window_end=start + timedelta(seconds=5))
    store = ElasticsearchFingerprintStore(FakeElastic())
    store.write(fp)
    assert store.window(fp.window_start, fp.window_end) == fp
    assert store.series(start, start + timedelta(seconds=5)) == [fp]
