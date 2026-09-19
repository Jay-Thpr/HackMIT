import json

import httpx

from faultline_telemetry.elasticsearch import HttpElasticsearchClient


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
