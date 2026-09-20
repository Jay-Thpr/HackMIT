import json
import logging

from faultline_contracts import Actor, AuditEvent, EventKind, JsonlSink, Stage
from faultline_product.adapters import CommandPager, PagingAuditSink, WebhookPager


def _page(incident="inc-1", kind=EventKind.page_human):
    return AuditEvent(incident_id=incident, stage=Stage.canary, kind=kind, actor=Actor.orchestrator,
                      summary="canary regression; page human", payload={}, action_id="a1")


def test_only_page_human_reaches_the_webhook(tmp_path):
    posts = []
    sink = PagingAuditSink(JsonlSink(tmp_path / "audit.jsonl"), WebhookPager("https://hooks.test/x", post=lambda u, b, t: posts.append((u, json.loads(b)))), log=logging.getLogger("t"))
    sink.write(_page(kind=EventKind.action_apply))
    sink.write(_page())
    assert len(posts) == 1 and posts[0][0] == "https://hooks.test/x"
    body = posts[0][1]
    assert body["incident_id"] == "inc-1" and body["stage"] == 7 and body["action_id"] == "a1"
    assert body["text"] == "[faultline] inc-1: canary regression; page human"
    assert body["report"] == "faultline report --incident inc-1"
    assert len(sink.query("inc-1")) == 2  # the audit is untouched by paging


def test_pager_failure_never_breaks_the_audit(tmp_path, caplog):
    def boom(*_):
        raise OSError("network down")

    sink = PagingAuditSink(JsonlSink(tmp_path / "audit.jsonl"), WebhookPager("https://hooks.test/x", post=boom), log=logging.getLogger("t"))
    with caplog.at_level(logging.WARNING):
        sink.write(_page())
    assert len(sink.query("inc-1")) == 1 and sink.paged == []
    assert "pager failed" in caplog.text and "OSError" in caplog.text and "network down" not in caplog.text


def test_command_pager_gets_json_on_stdin(tmp_path):
    runs = []
    pager = CommandPager(["notify", "--x"], run=lambda argv, **kw: runs.append((argv, json.loads(kw["input"]))))
    PagingAuditSink(JsonlSink(tmp_path / "audit.jsonl"), pager, log=logging.getLogger("t")).write(_page())
    assert runs[0][0] == ["notify", "--x"] and runs[0][1]["summary"] == "canary regression; page human"
