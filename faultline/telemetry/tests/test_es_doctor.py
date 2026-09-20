import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from faultline_contracts.audit import AUDIT_INDEX
from faultline_telemetry.store import FINGERPRINT_INDEX, production_filter


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

ENV_VARS = [
    "FAULTLINE_ELASTICSEARCH_URL",
    "FAULTLINE_ELASTICSEARCH_API_KEY",
    "FAULTLINE_ELASTICSEARCH_SETUP_API_KEY",
    "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL",
    "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_API_KEY",
    "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_SETUP_API_KEY",
    "FAULTLINE_ELASTICSEARCH_MIRROR_URL",
    "FAULTLINE_ELASTICSEARCH_MIRROR_API_KEY",
    "FAULTLINE_ELASTICSEARCH_MIRROR_SETUP_API_KEY",
    "FAULTLINE_MIRROR_OUTBOX_DIR",
]

NOW = datetime(2026, 9, 20, 0, 0, 10, tzinfo=UTC)
STAMP = "2026-09-20T00:00:00Z"

PRIMARY_ENV = {
    "FAULTLINE_ELASTICSEARCH_URL": "https://primary.test",
    "FAULTLINE_ELASTICSEARCH_API_KEY": "primary-private-key",
}
MIRROR_ENV = {
    "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL": "https://mirror.test",
    "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_API_KEY": "mirror-private-key",
}


def script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def template_response(path: str) -> httpx.Response:
    return httpx.Response(200, json={"index_templates": [{"name": path.rsplit("/", 1)[-1]}]})


def search_response(path: str, *, stamp: str = STAMP) -> httpx.Response:
    field = "window_end" if FINGERPRINT_INDEX in path else "ts"
    return httpx.Response(200, json={
        "timed_out": False,
        "_shards": {"failed": 0},
        "hits": {"hits": [{"_source": {field: stamp}}]},
    })


def make_handler(calls, *, template=template_response, search=search_response, stamp=STAMP):
    def respond(request: httpx.Request) -> httpx.Response:
        content = request.read()
        body = json.loads(content) if content else None
        calls.append((request, body))
        path = request.url.path
        if path.startswith("/_index_template/"):
            return template(path)
        if path.endswith("/_search"):
            return search(path, stamp=stamp)
        return httpx.Response(500, json={"error": "unexpected path"})
    return respond


def diagnose_with(env, calls, doctor, *, incident_id=None, require_mirror=False, now=NOW, **handler_kwargs):
    transport = httpx.MockTransport(make_handler(calls, **handler_kwargs))
    return doctor.diagnose(env, incident_id=incident_id, require_mirror=require_mirror, now=now, transport=transport)


def test_successful_diagnosis_is_read_only_scoped_and_sanitized(tmp_path):
    doctor = script("es_doctor")
    calls = []
    env = {**PRIMARY_ENV, **MIRROR_ENV, "FAULTLINE_MIRROR_OUTBOX_DIR": str(tmp_path / "outbox")}
    report = diagnose_with(env, calls, doctor, incident_id="inc-secret-1")

    assert report["ok"] and report["exit_code"] == 0
    assert report["read_only"] and report["incident_scoped"]
    check = report["primary"]["checks"]["production_fingerprints"]
    assert check["status"] == "present"
    assert check["latest_timestamp"] == "2026-09-20T00:00:00+00:00"
    assert check["timestamp_age_s"] == 10
    assert report["mirror"]["checks"]["audit_events"]["status"] == "present"

    rendered = json.dumps(report)
    for secret in ("primary-private-key", "mirror-private-key", "primary.test", "mirror.test", "inc-secret-1"):
        assert secret not in rendered

    assert len(calls) == 8
    per_host = {"primary.test": [], "mirror.test": []}
    for request, body in calls:
        per_host[request.url.host].append((request, body))
    for host, host_calls in per_host.items():
        assert [(c.method, c.url.path) for c, _ in host_calls] == [
            ("GET", f"/_index_template/{FINGERPRINT_INDEX}"),
            ("GET", f"/_index_template/{AUDIT_INDEX}"),
            ("POST", f"/{FINGERPRINT_INDEX}/_search"),
            ("POST", f"/{AUDIT_INDEX}/_search"),
        ]
    for request, _ in calls:
        assert "_refresh" not in request.url.path and request.method in ("GET", "POST")
    expected_key = {"primary.test": "primary-private-key", "mirror.test": "mirror-private-key"}
    for request, _ in calls:
        assert request.headers["Authorization"] == f"ApiKey {expected_key[request.url.host]}"

    c1_body = next(body for request, body in calls if request.url.host == "primary.test" and request.url.path == f"/{FINGERPRINT_INDEX}/_search")
    assert c1_body["size"] == 1 and c1_body["track_total_hits"] is False
    assert c1_body["_source"] == ["window_end"]
    assert c1_body["sort"] == [{"window_end": {"order": "desc", "unmapped_type": "date"}}]
    assert c1_body["query"]["bool"]["filter"] == [production_filter(), {"term": {"incident_id": "inc-secret-1"}}]
    c4_body = next(body for request, body in calls if request.url.host == "primary.test" and request.url.path == f"/{AUDIT_INDEX}/_search")
    assert c4_body["_source"] == ["ts"]
    assert c4_body["sort"] == [{"ts": {"order": "desc", "unmapped_type": "date"}}]
    assert c4_body["query"]["bool"]["filter"] == [{"term": {"incident_id": "inc-secret-1"}}]

    assert not (tmp_path / "outbox").exists()


def test_empty_environment_is_config_error():
    doctor = script("es_doctor")
    calls = []
    report = diagnose_with({}, calls, doctor)
    assert report["primary"]["configuration"] == "not_configured"
    assert report["mirror"]["configuration"] == "not_configured"
    assert report["exit_code"] == 2 and not report["ok"]
    assert calls == []


def test_primary_url_without_key_is_configured_and_keyless():
    doctor = script("es_doctor")
    calls = []
    report = diagnose_with({"FAULTLINE_ELASTICSEARCH_URL": "https://primary.test"}, calls, doctor)
    assert report["primary"]["configuration"] == "configured"
    assert report["exit_code"] == 0 and report["ok"]
    assert len(calls) == 4
    assert all("Authorization" not in request.headers for request, _ in calls)


def test_primary_key_without_url_is_incomplete():
    doctor = script("es_doctor")
    calls = []
    report = diagnose_with({"FAULTLINE_ELASTICSEARCH_API_KEY": "k"}, calls, doctor)
    assert report["primary"]["configuration"] == "incomplete"
    assert report["exit_code"] == 2
    assert calls == []


def test_absent_mirror_alone_does_not_fail():
    doctor = script("es_doctor")
    calls = []
    report = diagnose_with(dict(PRIMARY_ENV), calls, doctor)
    assert report["mirror"]["configuration"] == "not_configured"
    assert report["exit_code"] == 0 and report["ok"]


def test_require_mirror_flags_absent_mirror():
    doctor = script("es_doctor")
    calls = []
    report = doctor.diagnose(dict(PRIMARY_ENV), require_mirror=True, now=NOW, transport=httpx.MockTransport(make_handler(calls)))
    assert report["require_mirror"] and report["exit_code"] == 2
    assert len(calls) == 4


@pytest.mark.parametrize("mirror_env", [
    {"FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL": "https://mirror.test"},
    {"FAULTLINE_OBSERVABILITY_ELASTICSEARCH_API_KEY": "mirror-private-key"},
])
def test_partial_mirror_either_side_fails(mirror_env):
    doctor = script("es_doctor")
    calls = []
    report = diagnose_with({**PRIMARY_ENV, **mirror_env}, calls, doctor)
    assert report["mirror"]["configuration"] == "incomplete"
    assert report["exit_code"] == 2 and not report["ok"]
    assert all(request.url.host == "primary.test" for request, _ in calls)


def test_observability_alias_preferred_without_credential_mixing():
    doctor = script("es_doctor")
    calls = []
    env = {
        **PRIMARY_ENV,
        **MIRROR_ENV,
        "FAULTLINE_ELASTICSEARCH_MIRROR_URL": "https://legacy.test",
        "FAULTLINE_ELASTICSEARCH_MIRROR_API_KEY": "legacy-private-key",
    }
    report = diagnose_with(env, calls, doctor)
    assert report["exit_code"] == 0
    hosts = {request.url.host for request, _ in calls}
    assert hosts == {"primary.test", "mirror.test"}
    for request, _ in calls:
        if request.url.host == "mirror.test":
            assert request.headers["Authorization"] == "ApiKey mirror-private-key"


def test_incomplete_preferred_pair_does_not_fall_back_to_legacy():
    doctor = script("es_doctor")
    calls = []
    env = {
        **PRIMARY_ENV,
        "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL": "https://mirror.test",
        "FAULTLINE_ELASTICSEARCH_MIRROR_URL": "https://legacy.test",
        "FAULTLINE_ELASTICSEARCH_MIRROR_API_KEY": "legacy-private-key",
    }
    report = diagnose_with(env, calls, doctor)
    assert report["mirror"]["configuration"] == "incomplete"
    assert report["exit_code"] == 2
    assert all(request.url.host == "primary.test" for request, _ in calls)


def test_setup_key_is_never_used():
    doctor = script("es_doctor")
    calls = []
    env = {
        "FAULTLINE_ELASTICSEARCH_URL": "https://primary.test",
        "FAULTLINE_ELASTICSEARCH_SETUP_API_KEY": "setup-private-key",
        "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL": "https://mirror.test",
        "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_SETUP_API_KEY": "setup-private-key",
    }
    report = diagnose_with(env, calls, doctor)
    assert report["primary"]["configuration"] == "configured"
    assert report["mirror"]["configuration"] == "incomplete"
    assert report["exit_code"] == 2
    assert all("Authorization" not in request.headers for request, _ in calls)
    assert "setup-private-key" not in json.dumps(report)


@pytest.mark.parametrize("status,category", [
    (401, "unauthorized"),
    (403, "forbidden"),
    (404, "missing"),
    (429, "rate_limited"),
    (500, "http_error"),
])
def test_http_errors_map_each_check_independently(status, category):
    doctor = script("es_doctor")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, text="private-marker-body")

    report = doctor.diagnose(dict(PRIMARY_ENV), now=NOW, transport=httpx.MockTransport(respond))
    assert report["exit_code"] == 1 and not report["ok"]
    for name, check in report["primary"]["checks"].items():
        assert check["status"] == category, name
        assert check["http_status"] == status
    rendered = json.dumps(report)
    assert "private-marker-body" not in rendered
    assert all(check["status"] not in ("empty", "present") for check in report["primary"]["checks"].values())


def test_timeout_and_connection_errors_are_not_empty():
    doctor = script("es_doctor")
    for exc, expected in [
        (httpx.ConnectTimeout("private-marker-timeout"), "timeout"),
        (httpx.ReadTimeout("private-marker-timeout"), "timeout"),
        (httpx.ConnectError("private-marker-conn"), "unavailable"),
    ]:
        def respond(request, exc=exc):
            raise exc
        report = doctor.diagnose(dict(PRIMARY_ENV), now=NOW, transport=httpx.MockTransport(respond))
        assert report["exit_code"] == 1
        assert all(check["status"] == expected for check in report["primary"]["checks"].values())
        assert "private-marker" not in json.dumps(report)


def test_redirect_is_never_followed_to_second_origin():
    doctor = script("es_doctor")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.test/steal"})

    report = doctor.diagnose(dict(PRIMARY_ENV), now=NOW, transport=httpx.MockTransport(respond))
    assert report["exit_code"] == 1
    assert all(check["status"] == "http_error" for check in report["primary"]["checks"].values())
    assert all(request.url.host == "primary.test" for request in calls)


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="{not json"),
    httpx.Response(200, json=[1, 2, 3]),
    httpx.Response(200, json={"_shards": {"failed": 0}, "hits": {"hits": []}}),
    httpx.Response(200, json={"timed_out": False, "hits": {"hits": []}}),
    httpx.Response(200, json={"timed_out": False, "_shards": {"failed": 0}}),
    httpx.Response(200, json={"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [{"_source": {}}]}}),
    httpx.Response(200, json={"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [{"_source": {"window_end": "2026-09-20T00:00:00"}}]}}),
    httpx.Response(200, json={"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [{"_source": {"window_end": "not-a-date"}}]}}),
    httpx.Response(200, json={"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [{"_source": {"window_end": STAMP}}, {"_source": {"window_end": STAMP}}]}}),
    httpx.Response(200, json={"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [{"_source": "not-a-dict"}]}}),
    httpx.Response(200, json={"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [{"_source": {"window_end": 123}}]}}),
    httpx.Response(200, json={"timed_out": False, "_shards": {"failed": -1}, "hits": {"hits": [{"_source": {"window_end": STAMP}}]}}),
])
def test_malformed_search_responses_are_invalid_not_empty(response):
    doctor = script("es_doctor")

    def search(path, *, stamp=STAMP):
        return response

    calls = []
    report = diagnose_with(dict(PRIMARY_ENV), calls, doctor, search=search)
    checks = report["primary"]["checks"]
    assert checks["fingerprint_template"]["status"] == "present"
    assert checks["audit_template"]["status"] == "present"
    assert checks["production_fingerprints"]["status"] == "invalid_response"
    assert report["exit_code"] == 1 and not report["ok"]


@pytest.mark.parametrize("shard_payload", [
    {"timed_out": True, "_shards": {"failed": 0}, "hits": {"hits": [{"_source": {"window_end": STAMP}}]}},
    {"timed_out": False, "_shards": {"failed": 2}, "hits": {"hits": [{"_source": {"window_end": STAMP}}]}},
])
def test_partial_search_response_even_with_hits(shard_payload):
    doctor = script("es_doctor")

    def search(path, *, stamp=STAMP):
        payload = dict(shard_payload)
        if "fingerprints" not in path:
            payload = {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [{"_source": {"ts": stamp}}]}}
        return httpx.Response(200, json=payload)

    calls = []
    report = diagnose_with(dict(PRIMARY_ENV), calls, doctor, search=search)
    checks = report["primary"]["checks"]
    assert checks["production_fingerprints"]["status"] == "partial_response"
    assert checks["audit_events"]["status"] == "present"
    assert report["exit_code"] == 1


def test_empty_hits_are_empty_with_null_timestamp_and_age():
    doctor = script("es_doctor")

    def search(path, *, stamp=STAMP):
        return httpx.Response(200, json={"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": []}})

    calls = []
    report = diagnose_with(dict(PRIMARY_ENV), calls, doctor, search=search)
    check = report["primary"]["checks"]["production_fingerprints"]
    assert check == {"status": "empty", "latest_timestamp": None, "timestamp_age_s": None}
    assert report["exit_code"] == 1 and not report["ok"]


def test_future_timestamp_is_flagged_with_negative_age():
    doctor = script("es_doctor")
    calls = []
    report = diagnose_with(dict(PRIMARY_ENV), calls, doctor, stamp="2026-09-20T00:00:30Z")
    check = report["primary"]["checks"]["production_fingerprints"]
    assert check["status"] == "future_timestamp"
    assert check["timestamp_age_s"] == -20
    assert report["exit_code"] == 1 and not report["ok"]


def test_forbidden_templates_do_not_mask_healthy_searches():
    doctor = script("es_doctor")

    def template(path):
        return httpx.Response(403, text="private-marker-denied")

    calls = []
    report = diagnose_with(dict(PRIMARY_ENV), calls, doctor, template=template)
    checks = report["primary"]["checks"]
    assert checks["fingerprint_template"]["status"] == "forbidden"
    assert checks["audit_template"]["status"] == "forbidden"
    assert checks["production_fingerprints"]["status"] == "present"
    assert checks["audit_events"]["status"] == "present"
    assert not report["primary"]["ok"] and report["exit_code"] == 1
    assert "private-marker-denied" not in json.dumps(report)


def test_missing_index_and_empty_template_list_report_missing():
    doctor = script("es_doctor")
    calls = []

    def search_missing(path, *, stamp=STAMP):
        return httpx.Response(404, json={"error": "private-marker-missing"})

    report = diagnose_with(dict(PRIMARY_ENV), calls, doctor, search=search_missing)
    assert report["primary"]["checks"]["production_fingerprints"]["status"] == "missing"
    assert report["exit_code"] == 1
    assert "private-marker-missing" not in json.dumps(report)

    calls = []

    def template_empty(path):
        return httpx.Response(200, json={"index_templates": []})

    report = diagnose_with(dict(PRIMARY_ENV), calls, doctor, template=template_empty)
    assert report["primary"]["checks"]["fingerprint_template"]["status"] == "missing"
    assert report["exit_code"] == 1


def test_matching_endpoints_do_not_claim_parity_and_limitations_present():
    doctor = script("es_doctor")
    calls = []
    report = diagnose_with({**PRIMARY_ENV, **MIRROR_ENV}, calls, doctor)
    assert report["primary"]["checks"] == report["mirror"]["checks"]
    assert "parity_verified" not in json.dumps(report)
    assert any("not checked" in limitation for limitation in report["limitations"])
    assert len(report["limitations"]) == 4


def test_default_clock_samples_age_after_each_search(monkeypatch):
    doctor = script("es_doctor")

    class AdvancingDatetime(datetime):
        instant = datetime(2026, 9, 20, 0, 0, 0, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.instant

    monkeypatch.setattr(doctor, "datetime", AdvancingDatetime)

    def search(path, *, stamp=STAMP):
        AdvancingDatetime.instant = datetime(2026, 9, 20, 0, 0, 10, tzinfo=UTC)
        return search_response(path, stamp="2026-09-20T00:00:05Z")

    calls = []
    report = diagnose_with(dict(PRIMARY_ENV), calls, doctor, now=None, search=search)
    assert report["checked_at"] == "2026-09-20T00:00:10+00:00"
    for name in ("production_fingerprints", "audit_events"):
        check = report["primary"]["checks"][name]
        assert check["status"] == "present", name
        assert check["timestamp_age_s"] == 5, name
    assert report["exit_code"] == 0 and report["ok"]


def test_legacy_mirror_pair_is_used_and_keyed_correctly():
    doctor = script("es_doctor")
    calls = []
    env = {
        **PRIMARY_ENV,
        "FAULTLINE_ELASTICSEARCH_MIRROR_URL": "https://legacy.test",
        "FAULTLINE_ELASTICSEARCH_MIRROR_API_KEY": "legacy-private-key",
    }
    report = diagnose_with(env, calls, doctor)
    assert report["exit_code"] == 0 and report["ok"]
    assert report["mirror"]["configuration"] == "configured"
    assert len(calls) == 8
    legacy = [request for request, _ in calls if request.url.host == "legacy.test"]
    assert len(legacy) == 4
    assert all(request.headers["Authorization"] == "ApiKey legacy-private-key" for request in legacy)


@pytest.mark.parametrize("url", [
    "not a url",
    "ftp://primary.test",
    "https://user:private-marker@primary.test",
    "https://primary.test?secret=private-marker",
])
def test_invalid_endpoint_urls_are_config_errors_without_leaks(url):
    doctor = script("es_doctor")
    calls = []
    env = {"FAULTLINE_ELASTICSEARCH_URL": url, "FAULTLINE_ELASTICSEARCH_API_KEY": "primary-private-key"}
    report = diagnose_with(env, calls, doctor)
    assert report["primary"]["configuration"] == "invalid"
    assert report["exit_code"] == 2 and not report["ok"]
    assert calls == []
    rendered = json.dumps(report)
    assert "private-marker" not in rendered
    assert url not in rendered


def test_naive_now_raises_before_any_request():
    doctor = script("es_doctor")
    calls = []
    transport = httpx.MockTransport(make_handler(calls))
    with pytest.raises(ValueError):
        doctor.diagnose(dict(PRIMARY_ENV), now=datetime(2026, 9, 20), transport=transport)
    assert calls == []


def test_main_threads_require_mirror_and_returns_exit_code(monkeypatch, capsys):
    doctor = script("es_doctor")
    captured = {}

    def fake_diagnose(env, **kwargs):
        captured.update(kwargs)
        return {"exit_code": 2, "ok": False, "require_mirror": kwargs["require_mirror"], "incident_scoped": kwargs["incident_id"] is not None}

    monkeypatch.setattr(doctor, "load_repo_dotenv", lambda _: None)
    monkeypatch.setattr(doctor, "diagnose", fake_diagnose)
    assert doctor.main(["--require-mirror", "--incident-id", "inc-1"]) == 2
    assert captured == {"require_mirror": True, "incident_id": "inc-1"}
    output = json.loads(capsys.readouterr().out)
    assert output["exit_code"] == 2 and output["require_mirror"]


def test_main_end_to_end_closes_clients_on_success_and_failure(monkeypatch, capsys):
    doctor = script("es_doctor")
    monkeypatch.setattr(doctor, "load_repo_dotenv", lambda _: None)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FAULTLINE_ELASTICSEARCH_URL", "https://primary.test")
    monkeypatch.setenv("FAULTLINE_ELASTICSEARCH_API_KEY", "primary-private-key")

    real_client = httpx.Client
    clients = []

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(make_handler([]))
        client = real_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(doctor.httpx, "Client", factory)
    assert doctor.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] and output["exit_code"] == 0
    assert len(clients) == 1 and all(client.is_closed for client in clients)

    def failing_factory(**kwargs):
        def respond(request):
            return httpx.Response(500, text="private-marker")
        kwargs["transport"] = httpx.MockTransport(respond)
        client = real_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(doctor.httpx, "Client", failing_factory)
    assert doctor.main([]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["exit_code"] == 1
    assert "private-marker" not in capsys.readouterr().out + json.dumps(output)
    assert all(client.is_closed for client in clients)
