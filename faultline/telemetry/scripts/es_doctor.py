import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from faultline_contracts.audit import AUDIT_INDEX
from faultline_telemetry import load_repo_dotenv
from faultline_telemetry.store import FINGERPRINT_INDEX, production_filter


def latest_query(field: str, incident_id: str | None, *, production: bool) -> dict[str, Any]:
    filters = [production_filter()] if production else []
    if incident_id is not None:
        filters.append({"term": {"incident_id": incident_id}})
    return {
        "size": 1,
        "track_total_hits": False,
        "_source": [field],
        "query": {"bool": {"filter": filters}},
        "sort": [{field: {"order": "desc", "unmapped_type": "date"}}],
    }


def read_json(client: httpx.Client, path: str, body: dict[str, Any] | None = None):
    try:
        response = client.get(path) if body is None else client.post(path, json=body)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return None, {"status": "invalid_response"}
        return data, None
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        category = {401: "unauthorized", 403: "forbidden", 404: "missing", 429: "rate_limited"}.get(status, "http_error")
        return None, {"status": category, "http_status": status}
    except httpx.TimeoutException:
        return None, {"status": "timeout"}
    except httpx.RequestError:
        return None, {"status": "unavailable"}
    except (ValueError, TypeError):
        return None, {"status": "invalid_response"}


def template_check(client: httpx.Client, index: str) -> dict[str, Any]:
    data, error = read_json(client, f"/_index_template/{index}")
    if error is not None:
        return error
    templates = data.get("index_templates")
    if not isinstance(templates, list) or any(not isinstance(item, dict) for item in templates):
        return {"status": "invalid_response"}
    return {"status": "present" if any(item.get("name") == index for item in templates) else "missing"}


def latest_check(client: httpx.Client, index: str, field: str, incident_id: str | None, now: datetime | None, *, production: bool) -> dict[str, Any]:
    data, error = read_json(client, f"/{index}/_search", latest_query(field, incident_id, production=production))
    if error is not None:
        return error
    shards = data.get("_shards")
    if not isinstance(shards, dict) or type(shards.get("failed")) is not int or shards["failed"] < 0 or type(data.get("timed_out")) is not bool:
        return {"status": "invalid_response"}
    if data["timed_out"] or shards["failed"] > 0:
        return {"status": "partial_response"}
    hits = data.get("hits")
    hits = hits.get("hits") if isinstance(hits, dict) else None
    if not isinstance(hits, list) or len(hits) > 1:
        return {"status": "invalid_response"}
    if not hits:
        return {"status": "empty", "latest_timestamp": None, "timestamp_age_s": None}
    try:
        stamp = hits[0]["_source"][field]
        if not isinstance(stamp, str):
            raise ValueError()
        timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError()
        observed_at = datetime.now(UTC) if now is None else now
        age = (observed_at - timestamp).total_seconds()
        return {"status": "present" if age >= 0 else "future_timestamp", "latest_timestamp": timestamp.astimezone(UTC).isoformat(), "timestamp_age_s": age}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"status": "invalid_response"}


def inspect_endpoint(client: httpx.Client, incident_id: str | None, now: datetime | None) -> dict[str, Any]:
    checks = {
        "fingerprint_template": template_check(client, FINGERPRINT_INDEX),
        "audit_template": template_check(client, AUDIT_INDEX),
        "production_fingerprints": latest_check(client, FINGERPRINT_INDEX, "window_end", incident_id, now, production=True),
        "audit_events": latest_check(client, AUDIT_INDEX, "ts", incident_id, now, production=False),
    }
    return {"configuration": "configured", "checks": checks, "ok": all(check["status"] == "present" for check in checks.values())}


def endpoint_from_env(env, prefix: str, *, mirror: bool, incident_id: str | None, now: datetime | None, transport=None):
    url, key = env.get(f"{prefix}_URL"), env.get(f"{prefix}_API_KEY")
    if not url and not key:
        return {"configuration": "not_configured", "checks": {}, "ok": False}
    if not url or (mirror and not key):
        return {"configuration": "incomplete", "checks": {}, "ok": False}
    try:
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https") or not parsed.host or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
        headers = {"Authorization": f"ApiKey {key}"} if key else {}
        with httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=10.0, follow_redirects=False, transport=transport) as client:
            return inspect_endpoint(client, incident_id, now)
    except (httpx.InvalidURL, ValueError, TypeError):
        return {"configuration": "invalid", "checks": {}, "ok": False}


def diagnose(env, *, incident_id: str | None = None, require_mirror: bool = False, now: datetime | None = None, transport=None):
    if now is not None and now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    prefix = "FAULTLINE_OBSERVABILITY_ELASTICSEARCH"
    if not (env.get(f"{prefix}_URL") or env.get(f"{prefix}_API_KEY")):
        prefix = "FAULTLINE_ELASTICSEARCH_MIRROR"
    primary = endpoint_from_env(env, "FAULTLINE_ELASTICSEARCH", mirror=False, incident_id=incident_id, now=now, transport=transport)
    mirror = endpoint_from_env(env, prefix, mirror=True, incident_id=incident_id, now=now, transport=transport)
    mirror_required = require_mirror or mirror["configuration"] != "not_configured"
    config_error = primary["configuration"] != "configured" or (mirror_required and mirror["configuration"] != "configured")
    ok = primary["ok"] and (not mirror_required or mirror["ok"])
    return {
        "schema_version": "1",
        "checked_at": (datetime.now(UTC) if now is None else now).astimezone(UTC).isoformat(),
        "read_only": True,
        "incident_scoped": incident_id is not None,
        "require_mirror": require_mirror,
        "primary": primary,
        "mirror": mirror,
        "ok": ok,
        "exit_code": 2 if config_error else (0 if ok else 1),
        "limitations": [
            "Template presence does not verify mappings or write permissions.",
            "Latest document timestamps show stored evidence, not active ingestion or delivery lag.",
            "C1 checks exclude clones; C4 checks include all environments in the incident scope.",
            "Mirror parity, local outbox health, raw OTel, and Agent Builder are not checked.",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Elastic C1/C4 preflight; no writes, refreshes, or mirror workers.")
    parser.add_argument("--incident-id")
    parser.add_argument("--require-mirror", action="store_true")
    args = parser.parse_args(argv)
    load_repo_dotenv(Path(__file__))
    report = diagnose(os.environ, incident_id=args.incident_id, require_mirror=args.require_mirror)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
