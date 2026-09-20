import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from faultline_contracts import Fingerprint, ServiceStats
from faultline_contracts.fingerprint import SloStatus


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_kibana_objects_are_repeatable_and_keep_all_references_local():
    setup = script("kibana_setup")
    assert setup.build_ndjson() == setup.build_ndjson()
    objects = [json.loads(line) for line in setup.build_ndjson().splitlines()]
    identities = {(obj["type"], obj["id"]) for obj in objects}
    assert len(identities) == len(objects) == 9
    for obj in objects:
        for reference in obj["references"]:
            assert (reference["type"], reference["id"]) in identities
    traces = next(obj for obj in objects if obj["id"] == "faultline-otel-traces")
    assert traces["attributes"]["title"] == "traces-*.otel-*"
    production = next(obj for obj in objects if obj["id"] == "faultline-traces-production")
    query = production["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"]
    assert "deployment.environment.name" in query
    assert "deployment.environment :" in query


@pytest.mark.parametrize("result,expected", [(True, 0), (False, 1)])
def test_kibana_observability_selection_and_import_failure(monkeypatch, capsys, result, expected):
    setup = script("kibana_setup")
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.host == "observability.test"
        assert request.headers["Authorization"] == "ApiKey private-test-key"
        return httpx.Response(200, json={"success": result, "successResults": []})

    client = httpx.Client(base_url="https://observability.test", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(setup, "load_repo_dotenv", lambda _: None)
    monkeypatch.setenv("FAULTLINE_OBSERVABILITY_KIBANA_URL", "https://observability.test")
    monkeypatch.setenv("FAULTLINE_OBSERVABILITY_ELASTICSEARCH_SETUP_API_KEY", "private-test-key")
    monkeypatch.setattr(setup.sys, "argv", ["kibana_setup.py", "--observability"])

    def factory(**kwargs):
        client.headers.update(kwargs["headers"])
        return client

    monkeypatch.setattr(setup.httpx, "Client", factory)
    assert setup.main() == expected
    assert len(calls) == 1 and calls[0].url.params["overwrite"] == "true"
    assert "private-test-key" not in str(capsys.readouterr())
    assert client.is_closed


def test_centroid_missing_metrics_and_vote_ties_are_deterministic():
    check = script("ambiguity_check")
    centroids, scales = check.fit_centroids([("storm", {"db.qps": 100}), ("degraded", {"db.qps": 200})])
    assert check.predict_centroid({}, centroids, scales) == "unclassified"
    results = [("one", "storm", "storm"), ("one", "storm", "degraded")]
    assert check.score(results)["per_incident"] == check.score(list(reversed(results)))["per_incident"]
    assert check.score([])["balanced_accuracy"] is None
    assert check.score([("one", "storm", "storm")])["balanced_accuracy"] is None
    assert check.score([("one", "storm", "storm"), ("two", "degraded", "degraded")])["balanced_accuracy"] == 1


def test_ambiguity_uses_confirmed_production_labels_and_only_pre_action_windows():
    check = script("ambiguity_check")
    start = datetime(2026, 9, 20, tzinfo=UTC)
    first_apply = start + timedelta(seconds=20)

    def window(offset, breached):
        return Fingerprint(window_start=start + timedelta(seconds=offset), window_end=start + timedelta(seconds=offset + 5), services={"orders": ServiceStats(qps=80)}, slos=[SloStatus(name="checkout", metric="svc.orders.qps", threshold=60, value=80, breached=breached)]).model_dump(mode="json")

    class Client:
        def search(self, *, index, **_):
            if index == "faultline-audit":
                events = [
                    {"incident_id": "run", "kind": "detect", "ts": (start + timedelta(seconds=10)).isoformat(), "payload": {}, "environment": "production"},
                    {"incident_id": "run", "kind": "action_apply", "ts": first_apply.isoformat(), "payload": {}, "environment": "production"},
                    {"incident_id": "run", "kind": "verdict", "ts": (start + timedelta(seconds=40)).isoformat(), "actor": "math", "payload": {"diagnosis": "H_meta", "confirmed": True}, "environment": "production"},
                    {"incident_id": "run", "kind": "verdict", "ts": (start + timedelta(seconds=50)).isoformat(), "actor": "math", "payload": {"diagnosis": "H_db", "confirmed": True}, "environment": "clone", "clone_id": "c1"},
                ]
                return {"hits": {"hits": [{"_source": item} for item in events]}}
            return {"hits": {"hits": [{"_source": window(offset, hot)} for offset, hot in [(0, False), (10, True), (18, True)]]}}

    incidents = check.production_incidents(Client())
    assert len(incidents) == 1 and incidents[0]["label"] == "storm"
    assert len(incidents[0]["windows"]) == 1
    assert incidents[0]["windows"][0].window_end <= first_apply


def test_smoke_requiring_mirror_refuses_primary_only_before_writing(monkeypatch, capsys):
    smoke = script("es_smoke")

    class Primary:
        closed = False

        def close(self):
            self.closed = True

    primary = Primary()
    monkeypatch.setattr(smoke, "load_repo_dotenv", lambda _: None)
    monkeypatch.setattr(smoke, "client_from_env", lambda: primary)
    monkeypatch.setattr(smoke.sys, "argv", ["es_smoke.py", "--require-mirror"])
    assert smoke.main() == 2
    assert primary.closed
    assert "no smoke data written" in capsys.readouterr().err


def test_screenshot_credentials_are_restricted_to_the_kibana_origin(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    capture = script("kibana_screenshot")
    url = "https://kibana.test"
    headers = {"authorization": "old-key", "accept": "application/json"}
    own = capture.scoped_headers(url + "/api/status", url, "private-test-key", headers)
    assert own["Authorization"] == "ApiKey private-test-key"
    for other in ["https://cdn.test/script.js", "https://kibana.test:8443/api", "http://kibana.test/api"]:
        scoped = capture.scoped_headers(other, url, "private-test-key", headers)
        assert all(key.lower() != "authorization" for key in scoped)
        assert "private-test-key" not in str(scoped)


def test_clone_poller_does_not_duplicate_stats_path():
    collect = script("collect_clone_incident")
    poller = collect.ClonePoller({"orders": "http://orders/stats", "payments": "http://payments/", "loadgen": "http://loadgen"}, None, "sample", "clone-a")
    try:
        assert poller._urls == {"orders": "http://orders/stats", "payments": "http://payments/stats", "loadgen": "http://loadgen/stats"}
    finally:
        poller._http.close()
