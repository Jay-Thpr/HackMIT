"""Preview the five Owner 2 tools; --check reads Kibana, --apply explicitly mutates it."""

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from faultline_telemetry.agent_tools import (
    AgentToolsError,
    AgentToolsRegistry,
    tool_definitions,
    validate_tool_definition,
)
from faultline_telemetry.dotenv import load_repo_dotenv


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print fixed definitions offline (default); no auth or network.")
    mode.add_argument("--check", action="store_true", help="GET primary Kibana and print a reconciliation plan; no writes.")
    mode.add_argument("--apply", action="store_true", help="Explicitly authorize creating/updating the five fixed tool IDs.")
    args = parser.parse_args(argv)
    if not args.check and not args.apply:
        definitions = tool_definitions()
        for definition in definitions:
            validate_tool_definition(definition)
        print(json.dumps({"mode": "dry-run", "tools": definitions}, indent=2))
        return 0
    try:
        load_repo_dotenv(Path(__file__))
        url = os.environ.get("KIBANA_URL", "")
        key = next((os.environ[name] for name in (
            "KIBANA_API_KEY", "ELASTIC_AGENT_BUILDER_API_KEY", "FAULTLINE_ELASTICSEARCH_API_KEY"
        ) if os.environ.get(name)), "")
        with AgentToolsRegistry(url, key) as registry:
            plan = registry.reconcile(apply=args.apply)
    except AgentToolsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print("Agent tool setup failed (configuration)", file=sys.stderr)
        return 2
    print(json.dumps({"mode": "apply" if args.apply else "check", "tools": plan}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
