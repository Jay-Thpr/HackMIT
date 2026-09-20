"""One-time Elastic setup for the OTel sink: install the `faultline-otel` index template.

  uv run python scripts/otel_es_setup.py

The elasticsearch exporter (mapping mode `otel`) bulk-indexes into the `*-generic.otel-default`
data streams. Serverless vectordb projects ship no otel mappings, so this template supplies them
(`@timestamp: date_nanos`, `metrics` with subobjects:false, the named `metrics.*` dynamic templates
the exporter references per bulk item, strings -> keyword). Idempotent; run once per project before
the first export. Data streams auto-create on first write; existing streams with a stale mapping are
reported, never deleted.
"""

import json
import os
import sys
from pathlib import Path

import httpx

TEMPLATE_NAME = "faultline-otel"
TEMPLATE_PATH = Path(__file__).parent.parent / "otel" / "es-index-template.json"
DATA_STREAMS = ("traces-generic.otel-default", "metrics-generic.otel-default", "logs-generic.otel-default")


def root_env() -> None:
    for path in (Path(__file__).parent.parent.parent / ".env", Path(__file__).parent.parent / ".env"):
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k, v)
        except OSError:
            pass


def main() -> int:
    root_env()
    url = (os.environ.get("FAULTLINE_ELASTICSEARCH_URL") or "").rstrip("/")
    key = os.environ.get("FAULTLINE_ELASTICSEARCH_API_KEY")
    if not (url and key):
        print("FAIL  FAULTLINE_ELASTICSEARCH_URL / FAULTLINE_ELASTICSEARCH_API_KEY not set")
        return 1
    headers = {"Authorization": f"ApiKey {key}"}
    template = json.loads(TEMPLATE_PATH.read_text())
    try:
        before = httpx.get(f"{url}/_index_template/{TEMPLATE_NAME}", headers=headers, timeout=15)
        existed = before.status_code == 200 and before.json().get("index_templates")
        r = httpx.put(f"{url}/_index_template/{TEMPLATE_NAME}", headers=headers, timeout=15, json=template)
        r.raise_for_status()
        print(f"PASS  index template {TEMPLATE_NAME} {'updated' if existed else 'created'}")
    except httpx.HTTPError as e:
        detail = getattr(getattr(e, "response", None), "text", "")[:200]
        print(f"FAIL  PUT _index_template/{TEMPLATE_NAME}: {e} {detail}")
        return 1
    ok = True
    for ds in DATA_STREAMS:
        try:
            r = httpx.get(f"{url}/{ds}/_mapping", headers=headers, timeout=15)
            if r.status_code == 404:
                print(f"OK    {ds}: absent (auto-created on first export)")
                continue
            r.raise_for_status()
            ts = next(iter(r.json().values()))["mappings"]["properties"].get("@timestamp", {}).get("type")
            if ts == "date_nanos":
                print(f"PASS  {ds}: @timestamp={ts}")
            else:
                ok = False
                print(f"WARN  {ds}: @timestamp={ts} (expected date_nanos) — delete and recreate: "
                      f"DELETE _data_stream/{ds}")
        except httpx.HTTPError as e:
            ok = False
            print(f"FAIL  {ds}: {e}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
