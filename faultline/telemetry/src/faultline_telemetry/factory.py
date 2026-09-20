"""Build the Elasticsearch client from environment variables.

``FAULTLINE_ELASTICSEARCH_URL`` / ``FAULTLINE_ELASTICSEARCH_API_KEY`` configure
the authoritative primary (unset URL → ``None``, matching the "leave unset to
skip ES persistence" convention in .env.example). When
``FAULTLINE_ELASTICSEARCH_MIRROR_URL`` is also set, writes are additionally
mirrored to that second project — e.g. an Observability project holding the
OTel data — while reads stay on the primary.
"""

import os
from collections.abc import Mapping

from .elasticsearch import HttpElasticsearchClient, MirroredElasticsearchClient


def client_from_env(
    env: Mapping[str, str] | None = None,
) -> HttpElasticsearchClient | MirroredElasticsearchClient | None:
    env = os.environ if env is None else env
    url = env.get("FAULTLINE_ELASTICSEARCH_URL")
    if not url:
        return None
    primary = HttpElasticsearchClient(url, api_key=env.get("FAULTLINE_ELASTICSEARCH_API_KEY"), name="primary")
    mirror_url = env.get("FAULTLINE_ELASTICSEARCH_MIRROR_URL")
    if not mirror_url:
        return primary
    mirror = HttpElasticsearchClient(
        mirror_url, api_key=env.get("FAULTLINE_ELASTICSEARCH_MIRROR_API_KEY"), name="mirror"
    )
    return MirroredElasticsearchClient(primary, [mirror])
