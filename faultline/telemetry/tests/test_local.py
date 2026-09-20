import json
from datetime import datetime, timedelta, timezone

import pytest
from faultline_contracts import Fingerprint, ServiceStats

from faultline_telemetry.local import CorruptStoreError, JsonlFingerprintStore


def _fp(offset_s: int) -> Fingerprint:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset_s)
    return Fingerprint(window_start=start, window_end=start + timedelta(seconds=5),
                       services={"orders": ServiceStats(qps=1.0)})


def test_identity_and_time_bounds(tmp_path):
    store = JsonlFingerprintStore(tmp_path / "fp.jsonl")
    store.write(_fp(0), incident_id="i1")
    store.write(_fp(5), incident_id="i1", clone_id="c1")
    store.write(_fp(10), incident_id="i2")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=60)
    prod = store.query(start, end, incident_id="i1")
    assert len(prod) == 1
    assert prod[0].window_start == start
    assert len(store.query(start, end, incident_id="i1", clone_id="c1")) == 1
    assert len(store.query(start, end, incident_id="i2")) == 1
    assert store.query(start + timedelta(seconds=1), end, incident_id="i1") == []
    assert store.query(start, start + timedelta(seconds=4), incident_id="i1") == []
    assert len(store.query(start, start + timedelta(seconds=10), incident_id="i1")) == 1


def test_missing_file_and_truncated_tail(tmp_path):
    store = JsonlFingerprintStore(tmp_path / "fp.jsonl")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert store.query(start, start + timedelta(seconds=60)) == []
    store.write(_fp(0))
    with store._path.open("a") as fh:
        fh.write('{"schema_version": "faultline-local-c1/1", "incident')
    assert len(store.query(start, start + timedelta(seconds=60))) == 1


def test_corrupt_and_conflicting_rows(tmp_path):
    store = JsonlFingerprintStore(tmp_path / "fp.jsonl")
    store.write(_fp(0))
    with store._path.open("a") as fh:
        fh.write("garbage{}\n")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(CorruptStoreError):
        store.query(start, start + timedelta(seconds=60))

    path = tmp_path / "dup.jsonl"
    store2 = JsonlFingerprintStore(path)
    fp = _fp(0)
    store2.write(fp)
    changed = fp.model_copy(update={"services": {"orders": ServiceStats(qps=9.0)}})
    store2.write(changed)
    with pytest.raises(CorruptStoreError):
        store2.query(start, start + timedelta(seconds=60))
    store2.write(fp)
    rows = path.read_text().strip().split("\n")
    assert all(json.loads(r)["schema_version"] == "faultline-local-c1/1" for r in rows)
