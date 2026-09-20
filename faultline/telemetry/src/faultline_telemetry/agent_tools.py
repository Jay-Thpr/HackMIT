"""Fixed Owner 2 evidence tools for Agent Builder (9.4+ / current Serverless).

API: https://www.elastic.co/docs/explore-analyze/ai-features/agent-builder/kibana-api
Types: https://www.elastic.co/docs/explore-analyze/ai-features/agent-builder/tools/esql-tools
Only named value parameters are accepted; query structure is never caller input.
Missing indices or unmapped required fields are errors, not evidence of health.
"""

from copy import deepcopy
from datetime import datetime, timedelta
import math
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

OWNER2_TOOL_IDS = (
    "faultline.incident_timeline",
    "faultline.clone_vs_production",
    "faultline.similar_incidents",
    "faultline.incident_context",
)
TOOLS_ROUTE = "/api/agent_builder/tools"
MAX_WINDOW_DAYS = 90


def _param(kind: str, description: str) -> dict[str, Any]:
    return {"type": kind, "description": description, "optional": False}


_COMMON_PARAMS = {
    "incident_id": _param("string", "Incident identifier; required, never an index or query."),
    "environment": _param("string", "production or clone; comparisons/history require production."),
    "start": _param("date", "Inclusive UTC ISO-8601 start; window must be at most 90 days."),
    "end": _param("date", "Exclusive UTC ISO-8601 end, after start and at most 90 days later."),
}
_CLONE_PARAM = _param("string", "Exact clone ID for clone data; empty string for production-only reads.")
_GUARD = (
    '?incident_id != "" AND TO_DATETIME(?start) < TO_DATETIME(?end) '
    'AND TO_DATETIME(?end) <= TO_DATETIME(?start) + 90 days '
)
_ORIGIN = (
    '((?environment == "production" AND ?clone_id == "" '
    'AND environment == "production" AND clone_id IS NULL) '
    'OR (?environment == "clone" AND ?clone_id != "" '
    'AND environment == "clone" AND clone_id == ?clone_id)) '
)
_WINDOW = "AND window_start >= TO_DATETIME(?start) AND window_start < TO_DATETIME(?end) "
_METRICS = (
    "services.orders.qps, services.orders.retry_ratio, db.query_p99_ms"
)


def _definition(tool_id: str, description: str, query: str, **params: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": tool_id,
        "type": "esql",
        "description": description,
        "tags": ["faultline", "owner2", "evidence"],
        "configuration": {"query": query, "params": deepcopy({**_COMMON_PARAMS, **params})},
    }


_DEFINITIONS = (
    _definition(
        OWNER2_TOOL_IDS[0],
        "Read up to 200 chronological C1 windows for one incident and exact environment/clone. "
        "Observed metric subset: services.orders.qps = svc.orders.qps, "
        "services.orders.retry_ratio = svc.orders.retry_ratio, db.query_p99_ms. "
        "Missing values remain null; rows may be truncated. Primary production evidence is "
        "authoritative. This is retrieval, not a causal diagnosis or the Brain math judge.",
        "FROM faultline-fingerprints | WHERE " + _GUARD
        + "AND incident_id == ?incident_id AND " + _ORIGIN + _WINDOW
        + "| KEEP incident_id, environment, clone_id, window_start, window_end, " + _METRICS
        + " | SORT window_start ASC | LIMIT 200",
        clone_id=_CLONE_PARAM,
    ),
    _definition(
        OWNER2_TOOL_IDS[1],
        "Compare primary production with one exact clone for the same incident/time window. "
        "Set environment=production and provide a nonempty clone_id. Returns separate per-origin "
        "window counts, first/last times, observed metric means and non-null metric counts. "
        "Means are not time-aligned reproduction scores; unequal coverage must be reported. "
        "Production is authoritative; clone evidence cannot establish a diagnosis or replace Brain math.",
        "FROM faultline-fingerprints | WHERE " + _GUARD
        + 'AND incident_id == ?incident_id AND ?environment == "production" AND ?clone_id != "" '
        + 'AND ((environment == ?environment AND clone_id IS NULL) '
        + 'OR (environment == "clone" AND clone_id == ?clone_id)) ' + _WINDOW
        + "| STATS windows = COUNT(*), first_window = MIN(window_start), last_window = MAX(window_start), "
        "orders_qps_mean = AVG(services.orders.qps), orders_qps_count = COUNT(services.orders.qps), "
        "orders_retry_ratio_mean = AVG(services.orders.retry_ratio), "
        "orders_retry_ratio_count = COUNT(services.orders.retry_ratio), "
        "db_query_p99_ms_mean = AVG(db.query_p99_ms), db_query_p99_ms_count = COUNT(db.query_p99_ms) "
        "BY environment, clone_id | SORT environment ASC | LIMIT 2",
        clone_id=_CLONE_PARAM,
    ),
    _definition(
        OWNER2_TOOL_IDS[2],
        "Retrieve up to 20 prior production candidate windows, excluding the current incident. "
        "Requires environment=production and two actual observed current-incident C1 values from "
        "primary telemetry: svc.orders.retry_ratio and db.query_p99_ms, plus their observation time. "
        "Do not invent missing inputs. Candidates must share both metrics. retrieval_score is "
        "1 minus mean relative distance over ONLY these two metrics (denominator max(abs(a),abs(b),1)). "
        "This approximate two-metric ranking is not full fingerprint_similarity, may repeat incident "
        "IDs, and is never a causal diagnosis, verdict, or substitute for authoritative primary evidence.",
        "FROM faultline-fingerprints | WHERE " + _GUARD
        + 'AND ?environment == "production" AND environment == ?environment AND clone_id IS NULL '
        + "AND incident_id IS NOT NULL AND incident_id != ?incident_id " + _WINDOW
        + "AND TO_DATETIME(?end) <= TO_DATETIME(?observed_at) "
        "AND ?orders_retry_ratio >= 0.0 AND ?db_query_p99_ms >= 0.0 "
        "AND services.orders.retry_ratio IS NOT NULL AND db.query_p99_ms IS NOT NULL "
        "AND services.orders.retry_ratio >= 0.0 AND db.query_p99_ms >= 0.0 "
        "| EVAL retrieval_score = 1.0 - ("
        "ABS(services.orders.retry_ratio - ?orders_retry_ratio) / "
        "GREATEST(ABS(services.orders.retry_ratio), ABS(?orders_retry_ratio), 1.0) + "
        "ABS(db.query_p99_ms - ?db_query_p99_ms) / "
        "GREATEST(ABS(db.query_p99_ms), ABS(?db_query_p99_ms), 1.0)) / 2.0, compared_metrics = 2 "
        "| KEEP incident_id, environment, window_start, services.orders.retry_ratio, "
        "db.query_p99_ms, retrieval_score, compared_metrics "
        "| SORT retrieval_score DESC, incident_id ASC, window_start DESC | LIMIT 20",
        orders_retry_ratio=_param("float", "Observed nonnegative svc.orders.retry_ratio, not a hypothesis."),
        db_query_p99_ms=_param("float", "Observed nonnegative db.query_p99_ms, not a hypothesis."),
        observed_at=_param("date", "UTC timestamp of those current-incident observations; end must not exceed it."),
    ),
    _definition(
        OWNER2_TOOL_IDS[3],
        "Read up to 200 C4 audit metadata events for one incident/environment/clone in time order. "
        "Returns stages, actors, kinds and action/experiment IDs for phase context; no free-form "
        "payload, summary, hidden labels or controller state. Equal timestamps do not imply causal "
        "ordering. Missing/truncated events are incomplete context, not proof of no action. "
        "Primary production evidence remains authoritative; this tool does not judge causes.",
        "FROM faultline-audit | WHERE " + _GUARD
        + "AND incident_id == ?incident_id AND " + _ORIGIN
        + "AND ts >= TO_DATETIME(?start) AND ts < TO_DATETIME(?end) "
        "| KEEP incident_id, environment, clone_id, ts, event_id, stage, kind, actor, action_id, experiment_id "
        "| SORT ts ASC, event_id ASC | LIMIT 200",
        clone_id=_CLONE_PARAM,
    ),
)


def tool_definitions() -> list[dict[str, Any]]:
    """Return independent API create bodies; every parameter is mandatory."""
    return deepcopy(list(_DEFINITIONS))


def validate_tool_definition(definition: Mapping[str, Any]) -> None:
    """Fail closed: only the exact reviewed definitions may be registered."""
    if not any(definition == expected for expected in _DEFINITIONS):
        raise ValueError("tool definition is outside the fixed Owner 2 registry")


def query_request(tool_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Build a bounded Elasticsearch _query body for local read-only validation.

    Agent Builder binds these same named values server-side. Local checks are not
    its security boundary: fixed query structure and WHERE guards enforce scope.
    """
    definition = next((item for item in _DEFINITIONS if item["id"] == tool_id), None)
    if definition is None:
        raise ValueError("unknown Owner 2 tool")
    schema = definition["configuration"]["params"]
    if set(params) != set(schema):
        raise ValueError("exact tool parameter keys are required")
    dates = {}
    for name, spec in schema.items():
        value = params[name]
        if spec["type"] in {"string", "date"}:
            if not isinstance(value, str) or len(value) > 256:
                raise ValueError("invalid string parameter")
        if spec["type"] == "date":
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("invalid timestamp parameter") from None
            if parsed.utcoffset() != timedelta(0):
                raise ValueError("timestamps must be timezone-aware UTC")
            dates[name] = parsed
        if spec["type"] == "float":
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                raise ValueError("metric parameters must be finite nonnegative numbers")
    if not params["incident_id"].strip():
        raise ValueError("incident_id is required")
    if not timedelta(0) < dates["end"] - dates["start"] <= timedelta(days=MAX_WINDOW_DAYS):
        raise ValueError("invalid time window")
    if params["environment"] not in {"production", "clone"}:
        raise ValueError("invalid environment")
    if tool_id in {OWNER2_TOOL_IDS[1], OWNER2_TOOL_IDS[2]}:
        if params["environment"] != "production":
            raise ValueError("comparison/history requires production reference")
    if tool_id == OWNER2_TOOL_IDS[1]:
        if not params["clone_id"].strip():
            raise ValueError("comparison requires clone_id")
    elif "clone_id" in params:
        if (params["environment"] == "production" and params["clone_id"] != "") or (
            params["environment"] == "clone" and not params["clone_id"].strip()
        ):
            raise ValueError("clone_id does not match environment")
    if "observed_at" in dates and dates["end"] > dates["observed_at"]:
        raise ValueError("history must precede the observed current incident")
    return {"query": definition["configuration"]["query"], "params": [{name: params[name]} for name in schema]}


class AgentToolsError(RuntimeError):
    """Sanitized setup failure: never includes remote bodies, URLs or credentials."""


def canonical_kibana_url(url: str) -> str:
    """Accept only the configured HTTPS origin, optionally a Kibana space path."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment
        or not re.fullmatch(r"(?:/s/[a-zA-Z0-9_-]+)?/?", parsed.path)
    ):
        raise ValueError("KIBANA_URL must be an HTTPS origin or space URL")
    return url.rstrip("/")


class AgentToolsRegistry:
    """Reconcile only the four fixed IDs on a single primary Kibana endpoint."""

    def __init__(self, kibana_url: str, api_key: str, *, transport: httpx.BaseTransport | None = None):
        self._url = canonical_kibana_url(kibana_url)
        if not api_key or api_key != api_key.strip() or "\n" in api_key or "\r" in api_key:
            raise ValueError("a valid Kibana API key is required")
        self._client = httpx.Client(
            headers={"Authorization": f"ApiKey {api_key}", "kbn-xsrf": "true"},
            timeout=30, follow_redirects=False, trust_env=False, transport=transport,
        )

    def __enter__(self) -> "AgentToolsRegistry":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, route: str, body: dict[str, Any] | None = None) -> httpx.Response:
        try:
            response = self._client.request(method, self._url + route, json=body)
        except httpx.HTTPError:
            raise AgentToolsError("Kibana request failed (transport)") from None
        if not 200 <= response.status_code < 300:
            raise AgentToolsError(f"Kibana request failed (HTTP {response.status_code})")
        return response

    def reconcile(self, *, apply: bool = False) -> list[dict[str, str]]:
        """GET first; default plans only. Explicit apply POSTs missing and PUTs changed tools."""
        response = self._request("GET", TOOLS_ROUTE)
        try:
            payload = response.json()
            rows = payload["results"]
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in rows):
                raise ValueError
            existing = {row["id"]: row for row in rows}
            if len(existing) != len(rows):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise AgentToolsError("Kibana returned an invalid tools list schema") from None
        plan = []
        for definition in tool_definitions():
            validate_tool_definition(definition)
            current = existing.get(definition["id"])
            if current is not None and (current.get("type") != "esql" or current.get("readonly") is True):
                raise AgentToolsError("existing Owner 2 ID is not an editable ES|QL tool")
            operation = "create" if current is None else (
                "unchanged" if all(current.get(key) == value for key, value in definition.items()) else "update"
            )
            plan.append({"id": definition["id"], "operation": operation})
        if apply:
            for definition, item in zip(tool_definitions(), plan):
                if item["operation"] == "create":
                    self._request("POST", TOOLS_ROUTE, definition)
                elif item["operation"] == "update":
                    body = {key: definition[key] for key in ("description", "tags", "configuration")}
                    self._request("PUT", TOOLS_ROUTE + "/" + definition["id"], body)
        return plan
