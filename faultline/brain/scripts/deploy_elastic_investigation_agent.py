#!/usr/bin/env python3
"""Create the OpenAI inference endpoint and read-only Elastic agent.

Required environment variables:
  ELASTICSEARCH_URL or FAULTLINE_ELASTICSEARCH_URL
                     Elasticsearch endpoint
  KIBANA_URL         Kibana endpoint for the same deployment
  ELASTIC_AGENT_BUILDER_API_KEY or ELASTIC_API_KEY
                     key with manage_inference and Agent Builder management
  OPENAI_API_KEY     key stored by Elastic in the inference endpoint
  OPENAI_MODEL       an OpenAI chat model id selected by the team

Owner 2 must create the four narrowly-scoped custom tools before this script is
run. The script verifies their IDs exist, then creates or updates the agent.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from faultline_contracts.fingerprint import Fingerprint
from faultline_brain.elastic_investigation import (
    AGENT_ID,
    INFERENCE_ID,
    OWNER2_TOOL_IDS,
    agent_definition,
    assert_agent_boundary,
    fixture_evidence,
    openai_inference_definition,
)


def request(method: str, url: str, api_key: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"ApiKey {api_key}", "kbn-xsrf": "true"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urlopen(Request(url, data=data, headers=headers, method=method), timeout=30) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


def require(*names: str) -> str:
    value = next((os.environ.get(name) for name in names if os.environ.get(name)), None)
    if not value:
        raise SystemExit(f"one of {', '.join(names)} is required")
    return value.rstrip("/") if any(name.endswith("_URL") for name in names) else value


def fixture_boundary_check(root: Path) -> None:
    for name in ("fingerprint_storm.json", "fingerprint_degraded_db.json"):
        evidence = fixture_evidence(Fingerprint.model_validate_json((root / "contracts" / "fixtures" / name).read_text()))
        rendered = json.dumps(evidence).lower()
        if any(forbidden in rendered for forbidden in ("storm", "degraded", "fault", "world", "cpu_starve")):
            raise RuntimeError(f"{name} leaks a hidden-world marker into investigation evidence")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    definition = agent_definition()
    assert_agent_boundary(definition)
    root = Path(__file__).resolve().parents[3]
    fixture_boundary_check(root)

    if args.dry_run:
        endpoint = openai_inference_definition("${OPENAI_API_KEY}", "${OPENAI_MODEL}")
        print(json.dumps({"inference_endpoint": endpoint, "agent": definition}, indent=2))
        return

    elasticsearch_url = require("ELASTICSEARCH_URL", "FAULTLINE_ELASTICSEARCH_URL")
    kibana_url = require("KIBANA_URL")
    elastic_api_key = require("ELASTIC_AGENT_BUILDER_API_KEY", "ELASTIC_API_KEY")
    endpoint = openai_inference_definition(require("OPENAI_API_KEY"), require("OPENAI_MODEL"))

    request("PUT", f"{elasticsearch_url}/_inference/chat_completion/{INFERENCE_ID}", elastic_api_key, endpoint)
    available = request("GET", f"{kibana_url}/api/agent_builder/tools", elastic_api_key)
    ids = {tool["id"] for tool in available.get("results", [])}
    missing = set(OWNER2_TOOL_IDS) - ids
    if missing:
        raise SystemExit(f"Owner 2 read tools are missing: {', '.join(sorted(missing))}")

    agent_url = f"{kibana_url}/api/agent_builder/agents/{AGENT_ID}"
    try:
        request("GET", agent_url, elastic_api_key)
    except HTTPError as error:
        if error.code != 404:
            raise
        request("POST", f"{kibana_url}/api/agent_builder/agents", elastic_api_key, definition)
    else:
        request("PUT", agent_url, elastic_api_key, definition)
    deployed = request("GET", agent_url, elastic_api_key)
    assert_agent_boundary(deployed)
    print(f"deployed {AGENT_ID} using inference endpoint {INFERENCE_ID}")


if __name__ == "__main__":
    main()
