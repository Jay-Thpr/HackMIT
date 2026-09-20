"""Faultline Track 2: C1 telemetry and C4 Elasticsearch audit adapters."""

from .audit import ElasticsearchAuditSink
from .analytics import (
    ElasticsearchTelemetryAnalytics,
    FingerprintRecord,
    ReproductionSimilarity,
    SimilarIncident,
    fingerprint_similarity,
)
from .ambiguity import ambiguity_rows, export_ambiguity_window
from .config import TelemetrySettings
from .distributed import fingerprint_from_distributed
from .dotenv import load_repo_dotenv
from .elasticsearch import HttpElasticsearchClient
from .factory import client_from_env
from .mirror import MirroredElasticsearchClient
from .esql import incident_timeline
from .indices import ensure_index_templates
from .fingerprint import fingerprint_from_stats
from .local import CorruptStoreError, JsonlFingerprintStore
from .source import HttpSandboxStats, PollingTelemetrySource
from .store import ElasticsearchFingerprintStore, FINGERPRINT_INDEX
from .tokens import IncidentTokenComparison, compare_incident_tokens, fingerprint_prompt_payload, token_count

__all__ = [
    "ElasticsearchAuditSink", "ElasticsearchFingerprintStore", "ElasticsearchTelemetryAnalytics", "FINGERPRINT_INDEX", "FingerprintRecord", "HttpElasticsearchClient", "HttpSandboxStats",
    "MirroredElasticsearchClient", "PollingTelemetrySource", "TelemetrySettings", "ambiguity_rows", "client_from_env", "ensure_index_templates", "export_ambiguity_window", "fingerprint_from_stats",
    "fingerprint_from_distributed", "fingerprint_similarity", "incident_timeline", "load_repo_dotenv", "ReproductionSimilarity", "SimilarIncident",
    "CorruptStoreError", "JsonlFingerprintStore",
    "IncidentTokenComparison", "compare_incident_tokens", "fingerprint_prompt_payload", "token_count",
]
