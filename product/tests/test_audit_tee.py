import logging
from datetime import datetime, timezone

from faultline_contracts.audit import Actor, AuditEvent, EventKind, Stage, JsonlSink

from faultline_product.adapters import TeeAuditSink


def event():
    return AuditEvent(
        incident_id="inc-1",
        ts=datetime(2026, 9, 19, tzinfo=timezone.utc),
        stage=Stage.detect,
        kind=EventKind.detect,
        actor=Actor.math,
        summary="checkout breached",
    )


class RecordingSink:
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def write(self, event, *, clone_id=None):
        if self.fail:
            raise RuntimeError("elasticsearch unreachable")
        self.events.append((event, clone_id))


def test_tee_writes_both_sinks_and_queries_primary(tmp_path):
    primary = JsonlSink(tmp_path / "audit.jsonl")
    secondary = RecordingSink()
    sink = TeeAuditSink(primary, secondary, log=logging.getLogger("test"))

    sink.write(event(), clone_id="clone-1")

    assert secondary.events[0][1] == "clone-1"
    assert [e.incident_id for e in sink.query("inc-1")] == ["inc-1"]


def test_secondary_failure_does_not_propagate(tmp_path, caplog):
    primary = JsonlSink(tmp_path / "audit.jsonl")
    sink = TeeAuditSink(primary, RecordingSink(fail=True), log=logging.getLogger("test.tee"))

    with caplog.at_level(logging.WARNING, logger="test.tee"):
        sink.write(event())

    assert "secondary audit sink failed" in caplog.text
    assert [e.incident_id for e in sink.query("inc-1")] == ["inc-1"]
