import threading
import time

import pytest

from faultline_telemetry.mirror import MirroredElasticsearchClient


class Client:
    def __init__(self):
        self.documents = []
        self.fail = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def index(self, **kwargs):
        self.entered.set()
        self.release.wait(2)
        if self.fail:
            raise RuntimeError("credential must not appear in health")
        self.documents.append(kwargs)
        return {"result": "created"}

    def search(self, **kwargs):
        return {"primary": True}


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_secondary_network_never_blocks_primary_and_reads_stay_primary(tmp_path):
    primary, secondary = Client(), Client()
    secondary.release.clear()
    mirror = MirroredElasticsearchClient(primary, secondary, tmp_path / "outbox.sqlite", retry_s=0.02)
    try:
        started = time.monotonic()
        mirror.index(index="faultline-audit", document={"ts": "now"})
        assert secondary.entered.wait(1)
        mirror.index(index="faultline-audit", document={"ts": "later"})
        assert time.monotonic() - started < 1
        assert len(primary.documents) == 2
        assert mirror.search(index="x", query={}, sort=[]) == {"primary": True}
    finally:
        secondary.release.set()
        mirror.close()


def test_failures_visible_recoverable_across_restart_and_duplicate_safe(tmp_path, caplog):
    primary, secondary = Client(), Client()
    secondary.fail = True
    path = tmp_path / "outbox.sqlite"
    mirror = MirroredElasticsearchClient(primary, secondary, path, retry_s=0.02)
    mirror.index(index="faultline-audit", document={"ts": "now", "clone_id": "clone-a"})
    wait_for(lambda: mirror.health()["failures"] > 0)
    assert mirror.health()["pending"] == 1
    assert "credential must not" not in str(mirror.health()) + caplog.text
    mirror.close()
    secondary.fail = False
    recovered = MirroredElasticsearchClient(primary, secondary, path, retry_s=0.02)
    try:
        wait_for(lambda: recovered.health()["pending"] == 0)
        assert secondary.documents == primary.documents
    finally:
        recovered.close()


def test_failed_primary_is_not_mirrored(tmp_path):
    primary, secondary = Client(), Client()
    primary.fail = True
    mirror = MirroredElasticsearchClient(primary, secondary, tmp_path / "outbox.sqlite")
    try:
        with pytest.raises(RuntimeError):
            mirror.index(index="faultline-audit", document={"ts": "now"})
        assert mirror.health()["pending"] == 0
        assert not secondary.documents
    finally:
        mirror.close()


def test_duplicate_queue_entries_are_coalesced(tmp_path):
    primary, secondary = Client(), Client()
    mirror = MirroredElasticsearchClient(primary, secondary, tmp_path / "outbox.sqlite", start=False)
    try:
        for _ in range(3):
            mirror.index(index="faultline-audit", document={"ts": "now"})
        assert mirror.health()["pending"] == 1
    finally:
        mirror.close()


def test_bounded_outbox_reports_overflow_without_failing_primary(tmp_path, caplog):
    primary, secondary = Client(), Client()
    mirror = MirroredElasticsearchClient(primary, secondary, tmp_path / "outbox.sqlite", start=False, max_pending=1)
    try:
        mirror.index(index="faultline-audit", document={"ts": "first"})
        mirror.index(index="faultline-audit", document={"ts": "second"})
        assert len(primary.documents) == 2
        assert mirror.health()["pending"] == 1
        assert mirror.health()["rejected"] == 1
        assert "outbox enqueue rejected" in caplog.text
    finally:
        mirror.close()


def test_byte_bound_and_closed_worker(tmp_path):
    mirror = MirroredElasticsearchClient(Client(), Client(), tmp_path / "outbox.sqlite", max_bytes=3)
    mirror.index(index="faultline-audit", document={"ts": "large"})
    assert mirror.health()["rejected"] == 1
    mirror.close()
    assert not mirror._thread.is_alive()
    mirror.close()


def test_concurrent_clone_and_audit_writers_are_coordinated(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    mirror = MirroredElasticsearchClient(Client(), Client(), tmp_path / "outbox.sqlite", start=False)
    try:
        def write(n):
            mirror.index(index="faultline-fingerprints", document={
                "window_start": "2026-01-01T00:00:00Z", "window_end": "2026-01-01T00:00:05Z",
                "clone_id": f"clone-{n % 3}", "incident_id": "inc-1",
            })
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(write, range(30)))
        assert mirror.health()["pending"] == 3
    finally:
        mirror.close()


def test_existing_outbox_is_private(tmp_path):
    path = tmp_path / "outbox.sqlite"
    path.touch(mode=0o666)
    path.chmod(0o666)
    mirror = MirroredElasticsearchClient(Client(), Client(), path, start=False)
    try:
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        mirror.close()


def test_updated_document_while_delivery_inflight_is_not_lost(tmp_path):
    primary, secondary = Client(), Client()
    secondary.release.clear()
    mirror = MirroredElasticsearchClient(primary, secondary, tmp_path / "outbox.sqlite", retry_s=0.01)
    document = {"event_id": "event", "ts": "now", "environment": "clone", "clone_id": "c1", "summary": "first"}
    try:
        mirror.index(index="faultline-audit", document=document)
        assert secondary.entered.wait(1)
        document["summary"] = "updated"
        mirror.index(index="faultline-audit", document=document)
        document["summary"] = "caller mutation must not reach outbox"
        secondary.release.set()
        wait_for(lambda: mirror.health()["pending"] == 0)
        assert [d["document"]["summary"] for d in secondary.documents] == ["first", "updated"]
    finally:
        secondary.release.set()
        mirror.close()


def test_close_timeout_leaves_worker_to_release_resources(tmp_path):
    secondary = Client()
    secondary.release.clear()
    closed = threading.Event()
    secondary.close = closed.set
    mirror = MirroredElasticsearchClient(Client(), secondary, tmp_path / "outbox.sqlite")
    mirror.index(index="faultline-audit", document={"event_id": "event"})
    assert secondary.entered.wait(1)
    mirror.close(timeout_s=0.01)
    assert not closed.is_set()
    secondary.release.set()
    assert closed.wait(1)
    mirror._thread.join(1)
    assert not mirror._thread.is_alive()


def test_http_retry_after_accepted_but_lost_response_is_deduplicated(tmp_path):
    import httpx
    from faultline_telemetry.elasticsearch import HttpElasticsearchClient

    documents = {}
    calls = []
    def transport(request):
        documents[request.url.path] = request.content
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("secret endpoint credential")
        return httpx.Response(201, json={"result": "updated"})
    secondary = HttpElasticsearchClient("https://b.test", client=httpx.Client(
        base_url="https://b.test", transport=httpx.MockTransport(transport)))
    mirror = MirroredElasticsearchClient(Client(), secondary, tmp_path / "outbox.sqlite", retry_s=0.01)
    try:
        mirror.index(index="faultline-audit", document={"event_id": "event-1", "incident_id": "inc"})
        wait_for(lambda: len(calls) >= 2 and mirror.health()["pending"] == 0)
        assert len(documents) == 1
        assert all(request.method == "PUT" for request in calls)
    finally:
        mirror.close()


def test_non_document_operations_are_primary_only_and_cleanup_is_idempotent(tmp_path):
    from unittest.mock import Mock

    primary, secondary = Mock(), Mock()
    mirror = MirroredElasticsearchClient(primary, secondary, tmp_path / "outbox.sqlite", start=False)
    assert mirror.search(index="i", query={}, sort=[]) is primary.search.return_value
    assert mirror.esql("FROM i", params=[]) is primary.esql.return_value
    assert mirror.refresh("i") is primary.refresh.return_value
    assert mirror.put_index_template("i", {}) is primary.put_index_template.return_value
    assert not secondary.mock_calls
    assert mirror.health()["pending"] == 0
    mirror.close()
    mirror.close()
    primary.close.assert_called_once()
    secondary.close.assert_called_once()


def test_outbox_rejects_symlinks_without_touching_target(tmp_path):
    target = tmp_path / "target"
    target.write_text("untouched")
    target.chmod(0o644)
    path = tmp_path / "outbox.sqlite"
    path.symlink_to(target)
    with pytest.raises(OSError):
        MirroredElasticsearchClient(Client(), Client(), path)
    assert target.read_text() == "untouched"
    assert target.stat().st_mode & 0o777 == 0o644


def test_full_outbox_coalesces_updates_and_recovers_capacity(tmp_path):
    primary, secondary = Client(), Client()
    mirror = MirroredElasticsearchClient(primary, secondary, tmp_path / "outbox.sqlite", max_pending=1, start=False)
    try:
        mirror.index(index="faultline-audit", document={"event_id": "first", "summary": "old"})
        mirror.index(index="faultline-audit", document={"event_id": "first", "summary": "updated"})
        mirror.index(index="faultline-audit", document={"event_id": "second"})
        assert mirror.health()["pending"] == 1
        assert mirror.health()["rejected"] == 1
        mirror._thread.start()
        wait_for(lambda: mirror.health()["pending"] == 0)
        mirror.index(index="faultline-audit", document={"event_id": "second"})
        wait_for(lambda: mirror.health()["pending"] == 0)
        assert [item["document"] for item in secondary.documents] == [
            {"event_id": "first", "summary": "updated"}, {"event_id": "second"},
        ]
    finally:
        mirror.close()


@pytest.mark.parametrize("limit", ["rows", "bytes"])
def test_independent_writers_cannot_overbook_shared_outbox(tmp_path, limit):
    from concurrent.futures import ThreadPoolExecutor
    import json

    first_document = {"event_id": "first"}
    second_document = {"event_id": "other"}
    one_document_bytes = len(json.dumps(first_document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
    bounds = {"max_pending": 1} if limit == "rows" else {"max_bytes": one_document_bytes}
    primary = Client()
    path = tmp_path / "outbox.sqlite"
    first = MirroredElasticsearchClient(primary, Client(), path, start=False, **bounds)
    second = MirroredElasticsearchClient(primary, Client(), path, start=False, **bounds)
    first_counted, second_counted, release = threading.Event(), threading.Event(), threading.Event()

    def pause_first(statement):
        if statement.startswith("SELECT bytes FROM outbox"):
            first_counted.set()
            release.wait(2)

    def observe_second(statement):
        if statement.startswith("SELECT bytes FROM outbox"):
            second_counted.set()

    first._db.set_trace_callback(pause_first)
    second._db.set_trace_callback(observe_second)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(first.index, index="faultline-audit", document=first_document)
            try:
                assert first_counted.wait(1)
                two = pool.submit(second.index, index="faultline-audit", document=second_document)
                second_counted.wait(0.05)
            finally:
                release.set()
            assert one.result(timeout=2) == {"result": "created"}
            assert two.result(timeout=2) == {"result": "created"}
        assert len(primary.documents) == 2
        assert first.health()["pending"] == second.health()["pending"] == 1
        assert first.health()["pending_bytes"] == one_document_bytes
        assert first.health()["rejected"] + second.health()["rejected"] == 1
    finally:
        release.set()
        first.close()
        second.close()


def test_shared_outbox_update_at_capacity_and_rejection_rolls_back(tmp_path):
    primary = Client()
    path = tmp_path / "outbox.sqlite"
    first = MirroredElasticsearchClient(primary, Client(), path, start=False, max_pending=1)
    second = MirroredElasticsearchClient(primary, Client(), path, start=False, max_pending=1)
    try:
        first.index(index="faultline-audit", document={"event_id": "first", "summary": "old"})
        second.index(index="faultline-audit", document={"event_id": "first", "summary": "updated"})
        first.index(index="faultline-audit", document={"event_id": "second"})
        assert first.health()["pending"] == second.health()["pending"] == 1
        assert first.health()["rejected"] + second.health()["rejected"] == 1
        assert not first._db.in_transaction and not second._db.in_transaction
    finally:
        first.close()
        second.close()

    secondary = Client()
    third = MirroredElasticsearchClient(primary, secondary, path, retry_s=0.01)
    try:
        wait_for(lambda: third.health()["pending"] == 0)
        assert [item["document"] for item in secondary.documents] == [{"event_id": "first", "summary": "updated"}]
        third.index(index="faultline-audit", document={"event_id": "second"})
        wait_for(lambda: third.health()["pending"] == 0)
        assert [item["document"] for item in secondary.documents] == [
            {"event_id": "first", "summary": "updated"}, {"event_id": "second"},
        ]
        assert third.health()["rejected"] == 0
    finally:
        third.close()


def test_shared_outbox_lock_contention_does_not_fail_primary(tmp_path):
    primary = Client()
    path = tmp_path / "outbox.sqlite"
    first = MirroredElasticsearchClient(primary, Client(), path, start=False)
    second = MirroredElasticsearchClient(primary, Client(), path, start=False)
    try:
        first._db.execute("BEGIN IMMEDIATE")
        try:
            assert second.index(index="faultline-audit", document={"event_id": "blocked"}) == {"result": "created"}
        finally:
            first._db.rollback()
        assert [item["document"] for item in primary.documents] == [{"event_id": "blocked"}]
        assert second.health()["pending"] == 0
        assert second.health()["rejected"] == 1
        assert second.health()["last_error"] == "OperationalError"
        second.index(index="faultline-audit", document={"event_id": "retry"})
        assert second.health()["pending"] == 1
        assert second.health()["rejected"] == 1
        assert not first._db.in_transaction and not second._db.in_transaction
    finally:
        first.close()
        second.close()
