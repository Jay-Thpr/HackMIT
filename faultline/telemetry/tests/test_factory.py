from unittest.mock import Mock

import pytest

from faultline_telemetry import HttpElasticsearchClient, MirroredElasticsearchClient, client_from_env
from faultline_telemetry import factory
from faultline_telemetry.elasticsearch import MirroredElasticsearchClient as LegacyMirror


def test_public_mirror_is_one_implementation():
    from faultline_telemetry.mirror import MirroredElasticsearchClient as DurableMirror

    assert LegacyMirror is MirroredElasticsearchClient is DurableMirror


@pytest.mark.parametrize("env", [{}, {"FAULTLINE_ELASTICSEARCH_URL": ""}, {
    "FAULTLINE_ELASTICSEARCH_API_KEY": "secret", "FAULTLINE_ELASTICSEARCH_MIRROR_URL": "https://b.test",
}])
def test_no_primary_url_disables_persistence(env):
    assert client_from_env(env) is None


def test_primary_client_and_authentication():
    client = client_from_env({"FAULTLINE_ELASTICSEARCH_URL": "http://primary.test", "FAULTLINE_ELASTICSEARCH_API_KEY": "k1"})
    try:
        assert isinstance(client, HttpElasticsearchClient)
        assert client.name == "primary"
        assert client._client.base_url.host == "primary.test"
        assert client._client.headers["Authorization"] == "ApiKey k1"
    finally:
        client.close()


@pytest.mark.parametrize("prefix", ["FAULTLINE_ELASTICSEARCH_MIRROR", "FAULTLINE_OBSERVABILITY_ELASTICSEARCH"])
def test_mirror_aliases_are_durable_named_and_private(tmp_path, prefix):
    client = client_from_env({
        "FAULTLINE_ELASTICSEARCH_URL": "https://a.test", "FAULTLINE_ELASTICSEARCH_API_KEY": "k1",
        f"{prefix}_URL": "https://b.test", f"{prefix}_API_KEY": "k2",
        "FAULTLINE_MIRROR_OUTBOX_DIR": str(tmp_path),
    })
    try:
        assert isinstance(client, MirroredElasticsearchClient)
        assert client.primary.name == "primary"
        assert client.mirrors == [client.secondary]
        assert client.mirrors[0].name == "mirror"
        assert client.secondary._client.base_url.host == "b.test"
        assert client.secondary._client.headers["Authorization"] == "ApiKey k2"
        paths = list(tmp_path.glob("*.sqlite"))
        assert len(paths) == 1
        assert paths[0].stat().st_mode & 0o777 == 0o600
    finally:
        client.close()


def test_new_aliases_preferred_without_mixing_keys(tmp_path):
    env = {
        "FAULTLINE_ELASTICSEARCH_URL": "https://a.test",
        "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL": "https://new.test",
        "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_API_KEY": "new-key",
        "FAULTLINE_ELASTICSEARCH_MIRROR_URL": "https://old.test",
        "FAULTLINE_ELASTICSEARCH_MIRROR_API_KEY": "old-key",
        "FAULTLINE_MIRROR_OUTBOX_DIR": str(tmp_path),
    }
    client = client_from_env(env)
    try:
        assert client.secondary._client.base_url.host == "new.test"
        assert client.secondary._client.headers["Authorization"] == "ApiKey new-key"
    finally:
        client.close()
    del env["FAULTLINE_OBSERVABILITY_ELASTICSEARCH_API_KEY"]
    client = client_from_env(env)
    try:
        assert isinstance(client, HttpElasticsearchClient)
    finally:
        client.close()


@pytest.mark.parametrize("prefix", ["FAULTLINE_ELASTICSEARCH_MIRROR", "FAULTLINE_OBSERVABILITY_ELASTICSEARCH"])
@pytest.mark.parametrize("suffix", ["URL", "API_KEY"])
def test_partial_mirror_configuration_only_disables_mirror(prefix, suffix, caplog):
    client = client_from_env({"FAULTLINE_ELASTICSEARCH_URL": "https://a.test", f"{prefix}_{suffix}": "secret"})
    try:
        assert isinstance(client, HttpElasticsearchClient)
        assert "incomplete" in caplog.text
        assert "secret" not in caplog.text
    finally:
        client.close()


def test_outbox_failure_and_cleanup_failure_keep_primary(monkeypatch, caplog):
    primary, secondary = Mock(), Mock()
    secondary.close.side_effect = RuntimeError("secret")
    monkeypatch.setattr(factory, "HttpElasticsearchClient", Mock(side_effect=[primary, secondary]))
    monkeypatch.setattr(factory, "MirroredElasticsearchClient", Mock(side_effect=OSError("secret")))
    client = client_from_env({
        "FAULTLINE_ELASTICSEARCH_URL": "https://a.test",
        "FAULTLINE_ELASTICSEARCH_MIRROR_URL": "https://b.test",
        "FAULTLINE_ELASTICSEARCH_MIRROR_API_KEY": "secret",
    })
    assert client is primary
    secondary.close.assert_called_once()
    primary.close.assert_not_called()
    assert "OSError" in caplog.text and "RuntimeError" in caplog.text
    assert "secret" not in caplog.text


def test_legacy_list_constructor_uses_same_durable_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("FAULTLINE_MIRROR_OUTBOX_DIR", str(tmp_path))
    primary = HttpElasticsearchClient("https://a.test")
    secondary = HttpElasticsearchClient("https://b.test", name="mirror")
    client = LegacyMirror(primary, [secondary])
    try:
        assert client.mirrors == [secondary]
        assert client.health()["worker_alive"]
        assert len(list(tmp_path.glob("*.sqlite"))) == 1
    finally:
        client.close()


def test_factory_reads_environment_when_mapping_omitted(monkeypatch):
    monkeypatch.setenv("FAULTLINE_ELASTICSEARCH_URL", "")
    assert client_from_env() is None
