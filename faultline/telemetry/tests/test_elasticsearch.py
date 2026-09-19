import httpx

from faultline_telemetry.elasticsearch import HttpElasticsearchClient


def test_http_elasticsearch_client_uses_document_and_search_endpoints():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201 if request.url.path.endswith("_doc") else 200, json={"hits": {"hits": []}})

    client = HttpElasticsearchClient(
        "http://elastic.test",
        httpx.Client(base_url="http://elastic.test", transport=httpx.MockTransport(handler)),
    )
    client.index(index="faultline-fingerprints", document={"x": 1})
    assert client.search(index="faultline-fingerprints", query={"match_all": {}}, sort=[{"window_start": "asc"}]) == {"hits": {"hits": []}}
    assert [request.url.path for request in requests] == ["/faultline-fingerprints/_doc", "/faultline-fingerprints/_search"]
