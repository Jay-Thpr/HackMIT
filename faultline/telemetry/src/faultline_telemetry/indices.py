"""Index templates so Faultline's C1/C4 documents get stable, searchable mappings.

Without templates Elasticsearch guesses field types from the first document it
sees: a keyword-looking ``incident_id`` stays usable, but an integer-valued
``p99_ms`` locks the field to ``long`` and silently rejects later float values,
and unmapped ``*_ms`` fields can't be sorted or ranged on consistently.  The
templates are idempotent — ``ensure_index_templates`` is safe to call on every
CLI startup.
"""

from typing import Any, Protocol

from .store import FINGERPRINT_INDEX
from faultline_contracts.audit import AUDIT_INDEX

_NUMERIC_AS_DOUBLE = [
    {
        "ms_qps_as_double": {
            "match": "*_ms",
            "mapping": {"type": "double"},
        }
    },
    {
        "qps_as_double": {
            "match": "*_qps",
            "mapping": {"type": "double"},
        }
    },
    {
        "longs_as_double": {
            "match_mapping_type": "long",
            "mapping": {"type": "double"},
        }
    },
    {
        "doubles_as_double": {
            "match_mapping_type": "double",
            "mapping": {"type": "double"},
        }
    },
]

FINGERPRINT_TEMPLATE: dict[str, Any] = {
    "index_patterns": [f"{FINGERPRINT_INDEX}*"],
    "template": {
        "mappings": {
            "dynamic": True,
            "dynamic_templates": _NUMERIC_AS_DOUBLE,
            "properties": {
                "window_start": {"type": "date"},
                "window_end": {"type": "date"},
                "incident_id": {"type": "keyword"},
                "clone_id": {"type": "keyword"},
                "environment": {"type": "keyword"},
                "schema_version": {"type": "keyword"},
            },
        }
    },
}

AUDIT_TEMPLATE: dict[str, Any] = {
    "index_patterns": [f"{AUDIT_INDEX}*"],
    "template": {
        "mappings": {
            "dynamic": True,
            "dynamic_templates": _NUMERIC_AS_DOUBLE,
            "properties": {
                "ts": {"type": "date"},
                "incident_id": {"type": "keyword"},
                "clone_id": {"type": "keyword"},
                "environment": {"type": "keyword"},
                "event_id": {"type": "keyword"},
                "kind": {"type": "keyword"},
                "actor": {"type": "keyword"},
                "schema_version": {"type": "keyword"},
                "stage": {"type": "integer"},
            },
        }
    },
}


class _TemplateClient(Protocol):
    def put_index_template(self, name: str, body: dict[str, Any]) -> Any: ...


def ensure_index_templates(client: _TemplateClient) -> None:
    """PUT both composable index templates; safe to repeat on every startup."""
    client.put_index_template(FINGERPRINT_INDEX, FINGERPRINT_TEMPLATE)
    client.put_index_template(AUDIT_INDEX, AUDIT_TEMPLATE)
