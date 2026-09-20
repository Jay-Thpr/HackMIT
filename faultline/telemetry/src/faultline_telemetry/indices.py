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

INCIDENT_MEMORY_INDEX = "faultline-incident-memory"
"""Human-readable incident reports used only for operator-facing recall."""

JINA_EMBEDDING_INFERENCE_ID = ".jina-embeddings-v3"
"""Elastic Inference Service endpoint already provisioned in Elastic Cloud."""

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


# This deliberately has no C1 metric fields.  It is a separate, human-facing
# memory layer, so its semantic similarity score can never become verdict input.
INCIDENT_MEMORY_TEMPLATE: dict[str, Any] = {
    "index_patterns": [f"{INCIDENT_MEMORY_INDEX}*"],
    "template": {
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "incident_id": {"type": "keyword"},
                "created_at": {"type": "date"},
                "environment": {"type": "keyword"},
                "clone_id": {"type": "keyword"},
                "diagnosis": {"type": "keyword"},
                "content": {
                    "type": "semantic_text",
                    "inference_id": JINA_EMBEDDING_INFERENCE_ID,
                },
            },
        }
    },
}


class _TemplateClient(Protocol):
    def put_index_template(self, name: str, body: dict[str, Any]) -> Any: ...


def ensure_index_templates(client: _TemplateClient) -> None:
    """PUT Track 2 templates; safe to repeat on every startup."""
    client.put_index_template(FINGERPRINT_INDEX, FINGERPRINT_TEMPLATE)
    client.put_index_template(AUDIT_INDEX, AUDIT_TEMPLATE)
    client.put_index_template(INCIDENT_MEMORY_INDEX, INCIDENT_MEMORY_TEMPLATE)
