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
from .dotenv import load_repo_dotenv
from .elasticsearch import HttpElasticsearchClient
from .factory import client_from_env
from .mirror import MirroredElasticsearchClient
from .esql import incident_timeline
from .indices import ensure_index_templates
from .fingerprint import fingerprint_from_stats
from .source import HttpSandboxStats, PollingTelemetrySource
from .store import ElasticsearchFingerprintStore, FINGERPRINT_INDEX
from .tokens import IncidentTokenComparison, compare_incident_tokens, fingerprint_prompt_payload, token_count

__all__ = [
    "ElasticsearchAuditSink", "ElasticsearchFingerprintStore", "ElasticsearchTelemetryAnalytics", "FINGERPRINT_INDEX", "FingerprintRecord", "HttpElasticsearchClient", "HttpSandboxStats",
    "PollingTelemetrySource", "TelemetrySettings", "ambiguity_rows", "ensure_index_templates", "export_ambiguity_window", "fingerprint_from_stats",
    "fingerprint_similarity", "incident_timeline", "load_repo_dotenv", "ReproductionSimilarity", "SimilarIncident",
    "IncidentTokenComparison", "compare_incident_tokens", "fingerprint_prompt_payload", "token_count",
    "MirroredElasticsearchClient", "client_from_env",
]
