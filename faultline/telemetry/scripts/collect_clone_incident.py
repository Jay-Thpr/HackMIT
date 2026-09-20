"""Reproduce one hero world in a clean clone, persist its C1 windows, and run similar-incident search.

    cd faultline/telemetry && uv run python scripts/collect_clone_incident.py storm|degraded [--lab-url URL]

Never touches production or :9900. Creates one clone via C6, reproduces the world with the
documented recipe (storm: `db_latency 800 / ttl 20` then wait for the self-sustaining phase;
degraded: `db_capacity 40`), polls the clone's public `/stats` into C1 windows through
`fingerprint_from_stats`, writes them to `faultline-fingerprints` tagged with the clone id, and
asks `similar_incidents()` which prior *production* incident each incident window resembles.
Prior incidents are labelled from their C4 verdict (H_meta -> storm, H_db -> degraded).

Writes `docs/clone-<world>-<stamp>.json` (baseline + incident windows + similarity result); that
file is also an `--extra` input for `ambiguity_check.py`. Destroys the clone on exit.
"""

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from faultline_contracts.clone import CloneSpec, HttpCloneLab
from faultline_contracts.common import WINDOW_S
from faultline_contracts.fingerprint import Fingerprint

from faultline_telemetry import ElasticsearchFingerprintStore, HttpElasticsearchClient, client_from_env, load_repo_dotenv
from faultline_telemetry.analytics import ElasticsearchTelemetryAnalytics
from faultline_telemetry.fingerprint import fingerprint_from_stats

RECIPES = {
    "storm": {"action": "db_latency", "params": {"extra_ms": 800}, "ttl_s": 20, "settle_after_expiry_s": 10},
    "degraded": {"action": "db_capacity", "params": {"capacity_qps": 40}, "ttl_s": 240, "settle_after_apply_s": 10},
}
VERDICT_LABELS = {"H_meta": "storm", "H_db": "degraded"}
DOCS = Path(__file__).parent.parent / "docs"


def breached(fp: Fingerprint) -> bool:
    return any(s.breached for s in fp.slos)


def prior_labels(client: HttpElasticsearchClient) -> dict[str, str]:
    audit = client.search(index="faultline-audit", query={"term": {"kind": "verdict"}}, sort=[{"ts": "asc"}])
    labels: dict[str, str] = {}
    for hit in audit["hits"]["hits"]:
        src = hit["_source"]
        if src.get("environment", "production") == "production" and not src.get("clone_id"):
            confirmed = src.get("actor") == "math" and src.get("payload", {}).get("confirmed") is True
            labels[src["incident_id"]] = VERDICT_LABELS.get(src["payload"].get("diagnosis"), "unknown") if confirmed else "unknown"
    return labels


class ClonePoller:
    def __init__(self, stats_urls: dict[str, str], store: ElasticsearchFingerprintStore, incident_id: str, clone_id: str):
        self._urls = {k: v.rstrip("/") if v.rstrip("/").endswith("/stats") else v.rstrip("/") + "/stats" for k, v in stats_urls.items() if k in ("orders", "payments", "loadgen")}
        self._http = httpx.Client(timeout=5.0)
        self._store, self._incident_id, self._clone_id = store, incident_id, clone_id
        self._previous: dict[str, Any] | None = None
        self.windows: list[Fingerprint] = []

    def poll(self) -> Fingerprint | None:
        current = {name: self._http.get(url).raise_for_status().json() for name, url in self._urls.items()}
        if self._previous is None:
            self._previous = current
            return None
        end = datetime.now(UTC).replace(microsecond=0)
        fp = fingerprint_from_stats(self._previous, current, end - timedelta(seconds=WINDOW_S), end)
        self._previous = current
        self.windows.append(fp)
        self._store.write(fp, incident_id=self._incident_id, clone_id=self._clone_id)
        return fp


def run(world: str, lab_url: str, incident_windows: int, timeout_s: int) -> dict[str, Any]:
    load_repo_dotenv(Path(__file__))
    es = client_from_env()
    if es is None:
        raise ValueError("FAULTLINE_ELASTICSEARCH_URL is required")
    store, analytics = ElasticsearchFingerprintStore(es), ElasticsearchTelemetryAnalytics(es)
    labels = prior_labels(es)
    recipe = RECIPES[world]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    incident_id = f"o2-observation-{stamp}"
    lab = HttpCloneLab(lab_url)
    try:
        clone = lab.create(CloneSpec(name="telemetry-reproduction"))
    except Exception:
        es.close()
        raise
    print(f"clone {clone.clone_id} {clone.status.value} endpoints={clone.endpoints.stats_urls}")
    handle = None
    poller = None
    try:
        poller = ClonePoller(clone.endpoints.stats_urls, store, incident_id, clone.clone_id)
        # phase 1: healthy baseline
        while len(poller.windows) < 6:
            fp = poller.poll()
            if fp:
                print(f"  healthy  {fp.window_start.time()} breached={breached(fp)} retry={fp.services['orders'].retry_ratio}")
            time.sleep(WINDOW_S)
        baseline = [fp for fp in poller.windows if not breached(fp)]
        if not baseline:
            raise RuntimeError("clone never produced a healthy window")
        # phase 2: reproduce
        handle = lab.apply(clone.clone_id, recipe["action"], recipe["params"], recipe["ttl_s"])
        applied = datetime.now(UTC)
        settle_from = applied + timedelta(seconds=recipe.get("settle_after_expiry_s", 0) + (recipe["ttl_s"] if "settle_after_expiry_s" in recipe else recipe["settle_after_apply_s"]))
        print(f"applied {recipe['action']} {recipe['params']} ttl={recipe['ttl_s']} at {applied.time()}; incident windows from {settle_from.time()}")
        incident: list[Fingerprint] = []
        deadline = time.monotonic() + timeout_s
        while len(incident) < incident_windows and time.monotonic() < deadline:
            time.sleep(WINDOW_S)
            fp = poller.poll()
            if fp is None:
                continue
            phase = "incident" if fp.window_start >= settle_from else "trigger "
            if fp.window_start >= settle_from and breached(fp):
                incident.append(fp)
            o = fp.services["orders"]
            print(f"  {phase} {fp.window_start.time()} breached={breached(fp)} retry={o.retry_ratio} err={o.error_rate} db_qps={fp.db.qps if fp.db else None}")
        if not incident:
            raise RuntimeError(f"{world} did not reproduce within {timeout_s}s")
        es.refresh("faultline-fingerprints")
        # phase 3: similar-incident search against prior production incidents
        top1 = Counter()
        per_window = []
        best_by_incident: dict[str, list[float]] = defaultdict(list)
        for fp in incident:
            matches = analytics.similar_incidents(fp, limit=10)
            if not matches:
                continue
            top1[matches[0].incident_id] += 1
            per_window.append({"window_start": fp.window_start.isoformat(), "top": [(m.incident_id, round(m.score, 4)) for m in matches[:3]]})
            seen = set()
            for m in matches:
                if m.incident_id not in seen:
                    seen.add(m.incident_id)
                    best_by_incident[m.incident_id].append(m.score)
        same_class = sum(n for inc, n in top1.items() if labels.get(inc) == world)
        result = {
            "incident_id": incident_id, "label": world, "origin": "clone", "clone_id": clone.clone_id,
            "recipe": recipe, "prior_incident_labels": labels,
            "windows": [fp.model_dump(mode="json") for fp in incident],
            "baseline": baseline[-1].model_dump(mode="json"),
            "similar": {
                "top1_by_incident": dict(top1),
                "top1_same_class": f"{same_class}/{sum(top1.values())}",
                "mean_best_score_by_incident": {k: round(sum(v) / len(v), 4) for k, v in best_by_incident.items()},
                "per_window": per_window,
            },
        }
        print(f"\nsimilar_incidents top-1 per window: {dict(top1)}  same-class {result['similar']['top1_same_class']}")
        print("labels:", {k: labels.get(k, 'unknown') for k in top1})
        print("mean best score by incident:", result["similar"]["mean_best_score_by_incident"])
        DOCS.mkdir(parents=True, exist_ok=True)
        out = DOCS / f"clone-{world}-{stamp}.json"
        out.write_text(json.dumps(result, indent=2, default=str))
        print("wrote", out)
        return result
    finally:
        try:
            if handle is not None:
                try:
                    lab.undo(handle)
                except Exception as exc:  # already expired is fine
                    print("undo:", type(exc).__name__)
            info = lab.destroy(clone.clone_id)
            print(f"destroyed {clone.clone_id}: {info.status.value}")
        finally:
            if poller is not None:
                poller._http.close()
            es.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("world", choices=sorted(RECIPES))
    ap.add_argument("--lab-url", default="http://127.0.0.1:9910")
    ap.add_argument("--incident-windows", type=int, default=20)
    ap.add_argument("--timeout-s", type=int, default=240)
    args = ap.parse_args()
    if args.incident_windows < 1 or args.timeout_s < 1:
        ap.error("--incident-windows and --timeout-s must be positive")
    run(args.world, args.lab_url, args.incident_windows, args.timeout_s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
