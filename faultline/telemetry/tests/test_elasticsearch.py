import json

import httpx

from faultline_telemetry.elasticsearch import HttpElasticsearchClient, MirroredElasticsearchClient


def _client(requests: list[httpx.Request], api_key: str | None = None) -> HttpElasticsearchClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = 201 if request.url.path.endswith("_doc") else 200
        return httpx.Response(status, json={"hits": {"hits": []}, "columns": [], "values": []})

    return HttpElasticsearchClient(
        "http://elastic.test",
        api_key=api_key,
        client=httpx.Client(base_url="http://elastic.test", transport=httpx.MockTransport(handler)),
    )


def _mock_host(host: str, requests: list[httpx.Request], status: int = 200, name: str | None = None) -> HttpElasticsearchClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if status >= 400:
            return httpx.Response(status, json={"error": "boom"})
        code = 201 if request.url.path.endswith("_doc") else 200
        return httpx.Response(code, json={"host": host, "hits": {"hits": []}, "columns": [], "values": []})

    return HttpElasticsearchClient(
        f"http://{host}",
        client=httpx.Client(base_url=f"http://{host}", transport=httpx.MockTransport(handler)),
        name=name,
    )


def test_http_elasticsearch_client_uses_document_and_search_endpoints():
    requests = []
    client = _client(requests)
    client.index(index="faultline-fingerprints", document={"x": 1})
    assert client.search(index="faultline-fingerprints", query={"match_all": {}}, sort=[{"window_start": "asc"}]) == {"hits": {"hits": []}, "columns": [], "values": []}
    assert [request.url.path for request in requests] == ["/faultline-fingerprints/_doc", "/faultline-fingerprints/_search"]


def test_api_key_adds_authorization_header():
    requests = []
    _client(requests, api_key="k").index(index="i", document={})
    assert requests[0].headers["Authorization"] == "ApiKey k"


def test_no_api_key_sends_no_authorization_header():
    requests = []
    _client(requests).index(index="i", document={})
    assert "Authorization" not in requests[0].headers


def test_search_sends_explicit_size_not_es_default():
    requests = []
    _client(requests).search(index="i", query={"match_all": {}}, sort=[])
    assert json.loads(requests[0].content)["size"] == 10000


def test_esql_posts_to_query_endpoint_with_params():
    requests = []
    _client(requests).esql("FROM idx | LIMIT 1", params=[{"incident": "inc-1"}])
    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/_query"
    assert body == {"query": "FROM idx | LIMIT 1", "params": [{"incident": "inc-1"}]}


def test_put_index_template_hits_named_template_endpoint():
    requests = []
    _client(requests).put_index_template("faultline-fingerprints", {"index_patterns": ["x*"]})
    assert requests[0].method == "PUT"
    assert requests[0].url.path == "/_index_template/faultline-fingerprints"


def test_mirrored_index_writes_primary_then_mirror_and_returns_primary_result():
    primary_requests, mirror_requests = [], []
    client = MirroredElasticsearchClient(
        _mock_host("primary.test", primary_requests, name="primary"),
        [_mock_host("mirror.test", mirror_requests, name="mirror")],
    )
    result = client.index(index="faultline-fingerprints", document={"x": 1})
    assert result["host"] == "primary.test"
    assert [r.url.host for r in primary_requests + mirror_requests] == ["primary.test", "mirror.test"]
    assert [r.url.path for r in mirror_requests] == ["/faultline-fingerprints/_doc"]


def test_mirrored_index_mirror_failure_reports_on_error_and_keeps_primary_result():
    primary_requests, mirror_requests = [], []
    errors = []
    client = MirroredElasticsearchClient(
        _mock_host("primary.test", primary_requests, name="primary"),
        [_mock_host("mirror.test", mirror_requests, status=500, name="mirror")],
        on_error=lambda name, exc: errors.append((name, exc)),
    )
    result = client.index(index="faultline-audit", document={"x": 1})
    assert result["host"] == "primary.test"
    assert len(errors) == 1
    assert errors[0][0] == "mirror"
    assert isinstance(errors[0][1], Exception)


def test_mirrored_index_mirror_failure_logs_warning_without_on_error(caplog):
    import logging

    client = MirroredElasticsearchClient(
        _mock_host("primary.test", []),
        [_mock_host("mirror.test", [], status=500)],
    )
    with caplog.at_level(logging.WARNING):
        client.index(index="i", document={})
    assert "mirror-0 index failed" in caplog.text


def test_mirrored_index_primary_failure_propagates_and_skips_mirror():
    mirror_requests = []
    client = MirroredElasticsearchClient(
        _mock_host("primary.test", [], status=500),
        [_mock_host("mirror.test", mirror_requests)],
    )
    try:
        client.index(index="i", document={})
    except httpx.HTTPStatusError:
        pass
    else:
        raise AssertionError("primary failure should propagate")
    assert mirror_requests == []


def test_mirrored_search_and_esql_hit_primary_only():
    primary_requests, mirror_requests = [], []
    client = MirroredElasticsearchClient(
        _mock_host("primary.test", primary_requests),
        [_mock_host("mirror.test", mirror_requests)],
    )
    client.search(index="i", query={"match_all": {}}, sort=[])
    client.esql("FROM i | LIMIT 1")
    assert [r.url.path for r in primary_requests] == ["/i/_search", "/_query"]
    assert mirror_requests == []


def test_mirrored_put_index_template_and_refresh_hit_both_hosts():
    primary_requests, mirror_requests = [], []
    client = MirroredElasticsearchClient(
        _mock_host("primary.test", primary_requests),
        [_mock_host("mirror.test", mirror_requests)],
    )
    client.put_index_template("faultline-fingerprints", {"index_patterns": ["x*"]})
    client.refresh("faultline-fingerprints")
    assert [r.url.path for r in primary_requests] == [
        "/_index_template/faultline-fingerprints",
        "/faultline-fingerprints/_refresh",
    ]
    assert [r.url.path for r in mirror_requests] == [
        "/_index_template/faultline-fingerprints",
        "/faultline-fingerprints/_refresh",
    ]
