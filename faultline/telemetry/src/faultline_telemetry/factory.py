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
