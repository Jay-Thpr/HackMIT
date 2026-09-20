"""Build the Elasticsearch client from environment variables.

``FAULTLINE_ELASTICSEARCH_URL`` / ``FAULTLINE_ELASTICSEARCH_API_KEY`` configure
the authoritative primary (unset URL → ``None``, matching the "leave unset to
skip ES persistence" convention in .env.example). When an Observability project
is also configured — ``FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL`` and its API
key, or the equivalent ``FAULTLINE_ELASTICSEARCH_MIRROR_*`` pair — writes are
additionally delivered to that project as a display mirror, while reads stay on
the primary. Incomplete mirror configuration is reported and leaves the primary
alone rather than queueing evidence that could never be delivered.
"""

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from .elasticsearch import HttpElasticsearchClient
from .mirror import MirroredElasticsearchClient, mirror_outbox_path

log = logging.getLogger(__name__)


def client_from_env(
    env: Mapping[str, str] | None = None,
) -> HttpElasticsearchClient | MirroredElasticsearchClient | None:
    env = os.environ if env is None else env
    url = env.get("FAULTLINE_ELASTICSEARCH_URL")
    if not url:
        return None
    primary = HttpElasticsearchClient(
        url, api_key=env.get("FAULTLINE_ELASTICSEARCH_API_KEY"), name="primary"
    )
    prefix = "FAULTLINE_OBSERVABILITY_ELASTICSEARCH"
    if not (env.get(f"{prefix}_URL") or env.get(f"{prefix}_API_KEY")):
        prefix = "FAULTLINE_ELASTICSEARCH_MIRROR"
    mirror_url = env.get(f"{prefix}_URL")
    mirror_key = env.get(f"{prefix}_API_KEY")
    if not mirror_url or not mirror_key:
        if mirror_url or mirror_key:
            log.warning("Elasticsearch display mirror configuration incomplete; primary only")
        return primary
    secondary = None
    try:
        secondary = HttpElasticsearchClient(mirror_url, api_key=mirror_key, name="mirror")
        directory = env.get("FAULTLINE_MIRROR_OUTBOX_DIR") or Path.home() / ".local/state/faultline"
        return MirroredElasticsearchClient(primary, secondary, mirror_outbox_path(mirror_url, directory))
    except Exception as exc:
        if secondary is not None:
            try:
                secondary.close()
            except Exception as close_exc:
                log.warning("Elasticsearch display mirror cleanup failed (%s)", type(close_exc).__name__)
        log.warning("Elasticsearch display mirror disabled; primary only (%s)", type(exc).__name__)
        return primary
