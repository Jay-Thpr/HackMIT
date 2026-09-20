from faultline_telemetry import HttpElasticsearchClient, MirroredElasticsearchClient
from faultline_telemetry.factory import client_from_env


def test_client_from_env_returns_none_without_url():
    assert client_from_env({}) is None
    assert client_from_env({"FAULTLINE_ELASTICSEARCH_URL": ""}) is None


def test_client_from_env_url_only_returns_plain_client():
    client = client_from_env(
        {"FAULTLINE_ELASTICSEARCH_URL": "http://primary.test", "FAULTLINE_ELASTICSEARCH_API_KEY": "k1"}
    )
    assert isinstance(client, HttpElasticsearchClient)
    assert not isinstance(client, MirroredElasticsearchClient)
    assert client._client.base_url.host == "primary.test"
    assert client._client.headers["Authorization"] == "ApiKey k1"
    assert client.name == "primary"


def test_client_from_env_mirror_url_returns_mirrored_client():
    client = client_from_env(
        {
            "FAULTLINE_ELASTICSEARCH_URL": "http://primary.test",
            "FAULTLINE_ELASTICSEARCH_API_KEY": "k1",
            "FAULTLINE_ELASTICSEARCH_MIRROR_URL": "http://mirror.test",
            "FAULTLINE_ELASTICSEARCH_MIRROR_API_KEY": "k2",
        }
    )
    assert isinstance(client, MirroredElasticsearchClient)
    assert client.primary._client.base_url.host == "primary.test"
    assert client.primary._client.headers["Authorization"] == "ApiKey k1"
    assert client.primary.name == "primary"
    assert len(client.mirrors) == 1
    mirror = client.mirrors[0]
    assert mirror._client.base_url.host == "mirror.test"
    assert mirror._client.headers["Authorization"] == "ApiKey k2"
    assert mirror.name == "mirror"


def test_client_from_env_empty_mirror_url_returns_plain_client():
    client = client_from_env(
        {"FAULTLINE_ELASTICSEARCH_URL": "http://primary.test", "FAULTLINE_ELASTICSEARCH_MIRROR_URL": ""}
    )
    assert isinstance(client, HttpElasticsearchClient)
    assert not isinstance(client, MirroredElasticsearchClient)
