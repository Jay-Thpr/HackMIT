"""Create the Faultline Kibana evidence view (PRD DoD item 13).

Imports, with fixed ids so re-runs overwrite cleanly:

* data views ``faultline-otel-traces``, ``faultline-fingerprints``,
  ``faultline-audit``
* saved searches splitting ``traces-generic.otel-default`` by
  ``resource.attributes.deployment.environment`` (production vs clone-N),
  plus listing searches over the C1 fingerprint and C4 audit indices
* dashboard ``faultline-evidence`` with production and clone trace searches
  side by side and the fingerprint/audit searches below

    uv run python scripts/kibana_setup.py

Reads KIBANA_URL / FAULTLINE_ELASTICSEARCH_API_KEY from the environment
(falling back to the repo-root .env). Prints PASS/FAIL per object and the
dashboard URL.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from faultline_telemetry import load_repo_dotenv

TRACE_COLUMNS = [
    "resource.attributes.deployment.environment",
    "resource.attributes.service.name",
    "name",
    "duration",
    "trace_id",
    "status.code",
]
TRACE_SORT = [["@timestamp", "desc"]]


def search_object(
    obj_id: str,
    title: str,
    data_view: str,
    columns: list[str],
    sort: list[list[str]],
    kql: str | None = None,
) -> dict:
    search_source = {
        "query": {"query": kql or "", "language": "kuery"},
        "filter": [],
        "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
    }
    return {
        "type": "search",
        "id": obj_id,
        "attributes": {
            "title": title,
            "description": "",
            "columns": columns,
            "sort": sort,
            "hits": 0,
            "isTextBasedQuery": False,
            "usesAdHocDataView": False,
            "timeRestore": False,
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(search_source),
            },
        },
        "references": [
            {
                "id": data_view,
                "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
                "type": "index-pattern",
            }
        ],
    }


def data_view_object(obj_id: str, title: str, name: str, time_field: str) -> dict:
    return {
        "type": "index-pattern",
        "id": obj_id,
        "attributes": {
            "title": title,
            "name": name,
            "timeFieldName": time_field,
        },
        "references": [],
    }


def panel(panel_index: str, obj_id: str, x: int, y: int, w: int, h: int) -> tuple[dict, dict]:
    panel_json = {
        "type": "search",
        "gridData": {"x": x, "y": y, "w": w, "h": h, "i": panel_index},
        "panelIndex": panel_index,
        "embeddableConfig": {"enhancements": {}},
    }
    reference = {"id": obj_id, "name": f"{panel_index}:savedObjectRef", "type": "search"}
    return panel_json, reference


def dashboard_object() -> dict:
    panels = [
        panel("prod", "faultline-traces-production", 0, 0, 24, 12),
        panel("clones", "faultline-traces-clones", 24, 0, 24, 12),
        panel("fingerprints", "faultline-fingerprints-all", 0, 12, 24, 10),
        panel("audit", "faultline-audit-all", 24, 12, 24, 10),
    ]
    return {
        "type": "dashboard",
        "id": "faultline-evidence",
        "attributes": {
            "title": "Faultline — OTel evidence: production vs clone",
            "description": "Raw OTel spans from traces-generic.otel-default split by "
            "resource.attributes.deployment.environment.",
            "panelsJSON": json.dumps([p for p, _ in panels]),
            "optionsJSON": json.dumps(
                {"useMargins": True, "syncColors": False, "syncCursor": True, "hidePanelTitles": False}
            ),
            "timeRestore": False,
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps({"query": {"query": "", "language": "kuery"}, "filter": []})
            },
        },
        "references": [r for _, r in panels],
    }


def build_ndjson() -> str:
    objects = [
        data_view_object(
            "faultline-otel-traces",
            "traces-generic.otel-default",
            "Faultline OTel traces",
            "@timestamp",
        ),
        data_view_object(
            "faultline-fingerprints",
            "faultline-fingerprints",
            "Faultline C1 fingerprints",
            "window_start",
        ),
        data_view_object(
            "faultline-audit",
            "faultline-audit",
            "Faultline C4 audit",
            "ts",
        ),
        search_object(
            "faultline-traces-production",
            "Faultline traces — production",
            "faultline-otel-traces",
            TRACE_COLUMNS,
            TRACE_SORT,
            'resource.attributes.deployment.environment : "production"',
        ),
        search_object(
            "faultline-traces-clones",
            "Faultline traces — clones",
            "faultline-otel-traces",
            TRACE_COLUMNS,
            TRACE_SORT,
            "resource.attributes.deployment.environment : clone-*",
        ),
        search_object(
            "faultline-traces-all",
            "Faultline traces — all environments",
            "faultline-otel-traces",
            TRACE_COLUMNS,
            TRACE_SORT,
        ),
        search_object(
            "faultline-fingerprints-all",
            "Faultline C1 fingerprints",
            "faultline-fingerprints",
            ["environment", "incident_id", "clone_id", "slos", "services"],
            [["window_start", "desc"]],
        ),
        search_object(
            "faultline-audit-all",
            "Faultline C4 audit",
            "faultline-audit",
            ["incident_id", "stage", "kind", "actor"],
            [["ts", "desc"]],
        ),
        dashboard_object(),
    ]
    return "\n".join(json.dumps(o) for o in objects) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kibana-url", default=None, help="Override $KIBANA_URL")
    parser.add_argument(
        "--api-key-env",
        default="FAULTLINE_ELASTICSEARCH_API_KEY",
        help="Name of the env var holding the API key (default: FAULTLINE_ELASTICSEARCH_API_KEY)",
    )
    args = parser.parse_args()

    load_repo_dotenv(Path(__file__))
    kibana_url = (args.kibana_url or os.environ.get("KIBANA_URL", "")).rstrip("/")
    api_key = os.environ.get(args.api_key_env)
    if not kibana_url or not api_key:
        print(f"KIBANA_URL and {args.api_key_env} must be set", file=sys.stderr)
        return 2

    client = httpx.Client(
        base_url=kibana_url,
        headers={"Authorization": f"ApiKey {api_key}", "kbn-xsrf": "true"},
        timeout=60.0,
    )

    body = build_ndjson()
    resp = client.post(
        "/api/saved_objects/_import",
        params={"overwrite": "true", "createNewCopies": "false"},
        files={"file": ("faultline.ndjson", body, "application/ndjson")},
    )
    if resp.status_code != 200:
        print(f"FAIL import: HTTP {resp.status_code} {resp.text[:2000]}", file=sys.stderr)
        return 1

    result = resp.json()
    ok = True
    if result.get("success"):
        for r in result.get("successResults", []):
            print(f"PASS {r.get('type')}/{r.get('id')} ({r.get('meta', {}).get('title', '')})")
    else:
        for r in result.get("successResults", []):
            print(f"PASS {r.get('type')}/{r.get('id')} ({r.get('meta', {}).get('title', '')})")
        for e in result.get("errors", []):
            ok = False
            print(f"FAIL {e.get('type')}/{e.get('id')}: {json.dumps(e.get('error', {}))[:500]}")
        if not result.get("successResults") and not result.get("errors"):
            ok = False
            print(f"FAIL import response: {json.dumps(result)[:2000]}")

    print(f"Dashboard: {kibana_url}/app/dashboards#/view/faultline-evidence")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
