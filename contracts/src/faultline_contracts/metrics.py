"""Canonical metric key registry.

Keys:
  svc.<service>.<field>        field in SERVICE_FIELDS
  db.<field>                   field in DB_FIELDS
  edge.<src>.<dst>.<field>     field in EDGE_FIELDS
  slo.<name>.value
Service/SLO names are free-form (so the OTel Demo works), fields are fixed.
"""

import re

SERVICE_FIELDS = ("qps", "p50_ms", "p99_ms", "error_rate", "retry_ratio", "timeout_rate")
DB_FIELDS = ("qps", "query_p50_ms", "query_p99_ms", "pool_busy_ratio")
EDGE_FIELDS = ("qps", "p99_ms", "error_rate")
RESOURCE_FIELDS = ("lag_messages", "replication_lag_bytes", "outstanding", "oldest_pending_ms",
                   "cache_hit_ratio", "cpu_throttled_ratio", "ready_replicas", "desired_replicas",
                   "accepted_total", "completed_total", "completion_p99_ms")

_NAME = r"[a-z0-9_\-]+"
_PATTERN = re.compile(
    rf"^(svc\.{_NAME}\.({'|'.join(SERVICE_FIELDS)})"
    rf"|db\.({'|'.join(DB_FIELDS)})"
    rf"|edge\.{_NAME}\.{_NAME}\.({'|'.join(EDGE_FIELDS)})"
    rf"|slo\.{_NAME}\.value"
    rf"|resource\.{_NAME}\.({'|'.join(RESOURCE_FIELDS)}))$"
)


def is_valid_metric_key(key: str) -> bool:
    return bool(_PATTERN.match(key))


def unknown_metrics(keys: list[str], known: set[str] | None = None) -> list[str]:
    """Keys that are malformed, or (if `known` is given) absent from the live fingerprint."""
    return [k for k in keys if not is_valid_metric_key(k) or (known is not None and k not in known)]
