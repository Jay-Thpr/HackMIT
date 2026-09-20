#!/usr/bin/env bash
# Demo-safe fixture flow: no Docker, fault controller, or production lever is used.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AUDIT="${FAULTLINE_DEMO_AUDIT:-$ROOT/state/demo-audit.jsonl}"
INCIDENT="${FAULTLINE_DEMO_INCIDENT:-demo-storm}"
mkdir -p "$(dirname "$AUDIT")"
rm -f "$AUDIT"
(cd "$ROOT" && uv run faultline --audit-log "$AUDIT" watch --fixture storm --incident "$INCIDENT")
(cd "$ROOT/ui" && npm run build)
echo "Opening the recorded fixture replay at http://127.0.0.1:8010/?incident=$INCIDENT"
(sleep 1; open "http://127.0.0.1:8010/?incident=$INCIDENT" 2>/dev/null || true) &
cd "$ROOT"
exec uv run faultline --audit-log "$AUDIT" ui --host 127.0.0.1 --port 8010
