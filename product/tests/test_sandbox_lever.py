import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest
from faultline_contracts import ActionStatus, LeverError
from faultline_product.adapters.sandbox import SandboxLeverAdapter

NOW = datetime(2026, 9, 19, 15, 0, tzinfo=timezone.utc)


def test_apply_posts_json_and_parses_applied_at():
    calls = []

    def http(method, url, *, data=None, timeout):
        calls.append((method, url, data))
        return 200, {"applied_at": "2026-09-19T15:00:01Z"}

    adapter = SandboxLeverAdapter(clock=lambda: NOW, http=http)
    handle = adapter.apply("retry_cap", {"max_retries": 0}, 20)

    method, url, data = calls[0]
    assert method == "POST"
    assert url.endswith("/admin/retry_override")
    assert json.loads(data) == {"max_retries": 0, "ttl_s": 20}
    assert handle.lever_id == "retry_cap"
    assert handle.params == {"max_retries": 0}
    assert handle.ttl_s == 20
    assert handle.applied_at == NOW + timedelta(seconds=1)


def test_apply_reports_control_service_conflict():
    def http(method, url, *, data=None, timeout):
        return 409, {"detail": "orders-v2 is not running"}

    adapter = SandboxLeverAdapter(http=http)
    with pytest.raises(LeverError, match="409"):
        adapter.apply("canary_weight", {"v2_weight": 0.05}, 300)


def test_apply_rejects_bad_params_before_http():
    calls = []

    def http(method, url, *, data=None, timeout):
        calls.append((method, url, data))
        return 200, {}

    adapter = SandboxLeverAdapter(http=http)
    with pytest.raises(LeverError):
        adapter.apply("retry_cap", {"max_retries": 7}, 20)
    assert calls == []


def test_undo_marks_action_undone_without_status_request():
    calls = []

    def http(method, url, *, data=None, timeout):
        calls.append((method, url, data))
        if method == "POST":
            return 200, {"applied_at": "2026-09-19T15:00:00Z"}
        if method == "DELETE":
            return 200, {}
        raise AssertionError("status should use the local undone set")

    adapter = SandboxLeverAdapter(clock=lambda: NOW, http=http)
    handle = adapter.apply("retry_cap", {"max_retries": 0}, 20)
    undone = adapter.undo(handle)

    assert calls[1][0] == "DELETE"
    assert calls[1][1].endswith("/admin/retry_override")
    assert undone.status == ActionStatus.undone
    assert adapter.status(handle) == ActionStatus.undone


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"active": True, "expires_at": "2026-09-19T15:00:20Z"}, ActionStatus.active),
        ({"active": False, "expires_at": "2026-09-19T14:59:00Z"}, ActionStatus.expired),
        ({"active": False, "expires_at": None}, ActionStatus.undone),
    ],
)
def test_status_maps_control_service_state(entry, expected):
    def http(method, url, *, data=None, timeout):
        if method == "POST":
            return 200, {"applied_at": "2026-09-19T15:00:00Z"}
        return 200, {"retry_cap": entry}

    adapter = SandboxLeverAdapter(clock=lambda: NOW, http=http)
    handle = adapter.apply("retry_cap", {"max_retries": 0}, 20)
    assert adapter.status(handle) == expected


def test_connection_refused_becomes_lever_error():
    def http(method, url, *, data=None, timeout):
        raise urllib.error.URLError("refused")

    adapter = SandboxLeverAdapter(http=http)
    with pytest.raises(LeverError, match="control service unreachable"):
        adapter.apply("retry_cap", {"max_retries": 0}, 20)
