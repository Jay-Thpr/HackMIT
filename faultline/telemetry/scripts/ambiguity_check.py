"""PRD ambiguity check: can passive methods tell storm from degraded DB on real fingerprints?

    cd faultline/telemetry && uv run python scripts/ambiguity_check.py [--llm] [--extra FILE ...]

Data: production C1 windows in `faultline-fingerprints` between the incident's C4 `detect`
event and its first `action_apply` (the passive triage phase, before any lever touched the
system). Labels come from the C4 `verdict` (H_meta -> storm, H_db -> degraded), i.e. from the
active experiment, never from telemetry. Incidents whose verdict was none-of-the-above or that
never reached a verdict are excluded. `--extra FILE` adds incidents collected from clones
(`collect_clone_incident.py`), labelled by the C6 action that produced them.

Classifiers see only `ambiguity_rows()` output (label-free canonical metrics):
  * nearest centroid, leave-one-incident-out (same normalisation as bench/baselines.py);
  * passive LLM (OpenAI, strict JSON): one healthy baseline window + one incident window,
    no experiment, asked to pick storm or degraded.
Chance is 50 %. Balanced accuracy (mean per-class recall) is the headline because the
production set is storm-heavy.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
from typing import Any

import httpx
from faultline_contracts.fingerprint import Fingerprint

from faultline_telemetry import HttpElasticsearchClient, load_repo_dotenv
from faultline_telemetry.ambiguity import ambiguity_rows

LABELS = {"H_meta": "storm", "H_db": "degraded"}
OUT = Path(__file__).parent.parent / "docs" / "ambiguity_report.json"


def _fp(doc: dict[str, Any]) -> Fingerprint:
    return Fingerprint.model_validate({k: v for k, v in doc.items() if k in Fingerprint.model_fields})


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def production_incidents(client: HttpElasticsearchClient) -> list[dict[str, Any]]:
    audit = [h["_source"] for h in client.search(index="faultline-audit", query={"match_all": {}}, sort=[{"ts": "asc"}])["hits"]["hits"]]
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in audit:
        events[event["incident_id"]].append(event)
    incidents = []
    for incident_id, evs in events.items():
        verdicts = [e["payload"].get("diagnosis") for e in evs if e["kind"] == "verdict"]
        detects = [e["ts"] for e in evs if e["kind"] == "detect"]
        applies = [e["ts"] for e in evs if e["kind"] == "action_apply"]
        if not detects or not applies or not verdicts or verdicts[-1] not in LABELS:
            continue
        first_apply = _ts(min(applies))
        docs = [
            h["_source"]
            for h in client.search(
                index="faultline-fingerprints",
                query={"bool": {"filter": [{"term": {"incident_id": incident_id}}, {"term": {"environment": "production"}}]}},
                sort=[{"window_start": "asc"}],
            )["hits"]["hits"]
        ]
        fps = [_fp(d) for d in docs]
        # Product stops polling production while the clone investigators run, so the passive-phase
        # windows are the breached ones between SLO breach onset and the first lever (detect sits inside).
        windows = [f for f in fps if f.window_start < first_apply and any(s.breached for s in f.slos)]
        healthy = [f for f in fps if windows and f.window_end <= windows[0].window_start and f.slos and not any(s.breached for s in f.slos)]
        if not windows or not healthy:
            continue
        incidents.append({"incident_id": incident_id, "label": LABELS[verdicts[-1]], "origin": "production", "windows": windows, "baseline": healthy[-1]})
    return incidents


def extra_incidents(paths: list[str]) -> list[dict[str, Any]]:
    out = []
    for path in paths:
        data = json.loads(Path(path).read_text())
        out.append({
            "incident_id": data["incident_id"], "label": data["label"], "origin": data.get("origin", "clone"),
            "windows": [Fingerprint.model_validate(w) for w in data["windows"]],
            "baseline": Fingerprint.model_validate(data["baseline"]),
        })
    return out


# --- nearest centroid (mirrors bench/src/faultline_bench/baselines.py) ---------------------

def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def fit_centroids(cases: list[tuple[str, dict[str, float]]]) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    all_values: dict[str, list[float]] = defaultdict(list)
    for label, metrics in cases:
        for key, value in metrics.items():
            grouped[label][key].append(value)
            all_values[key].append(value)
    centroids = {label: {k: sum(v) / len(v) for k, v in m.items()} for label, m in grouped.items()}
    scales = {k: max(_std(v), abs(sum(v) / len(v)) * 0.10, 1e-9) for k, v in all_values.items()}
    return centroids, scales


def predict_centroid(metrics: dict[str, float], centroids: dict[str, dict[str, float]], scales: dict[str, float]) -> str:
    best = []
    for label, centroid in centroids.items():
        common = sorted(set(metrics) & set(centroid))
        if common:
            best.append((sqrt(sum(((metrics[k] - centroid[k]) / scales[k]) ** 2 for k in common) / len(common)), label))
    return min(best)[1]


# --- passive LLM ------------------------------------------------------------------------------

SYSTEM = (
    "You are an SRE triaging a checkout system: gateway -> orders -> payments -> Postgres. Orders retries "
    "payments calls (up to 3 retries, 500 ms attempt timeout). You get canonical 5 s telemetry windows: a healthy "
    "baseline and the current incident window. Decide, from telemetry alone and without running any experiment, "
    "which single cause is present:\n"
    "- storm: a self-sustaining retry storm. A transient slowdown ended; the database itself is healthy now but "
    "retry amplification keeps it saturated.\n"
    "- degraded: the database is persistently degraded (lost capacity); the retries are a symptom, not the cause.\n"
    "Answer with JSON {\"diagnosis\": \"storm\" | \"degraded\"}."
)
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "passive_diagnosis", "strict": True,
        "schema": {"type": "object", "additionalProperties": False, "required": ["diagnosis"],
                   "properties": {"diagnosis": {"type": "string", "enum": ["storm", "degraded"]}}},
    },
}


def llm_predict(client: httpx.Client, model: str, baseline: dict[str, float], window: dict[str, float]) -> str:
    body = {
        "model": model, "temperature": 0, "response_format": RESPONSE_FORMAT,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps({"healthy_baseline": baseline, "incident_window": window}, sort_keys=True)},
        ],
    }
    r = client.post("/v1/chat/completions", json=body, timeout=60)
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])["diagnosis"]


# --- scoring --------------------------------------------------------------------------------

def score(results: list[tuple[str, str, str]]) -> dict[str, Any]:
    """results: (incident_id, label, prediction) per window."""
    per_class: dict[str, list[bool]] = defaultdict(list)
    per_incident: dict[str, tuple[str, list[str]]] = {}
    for incident_id, label, pred in results:
        per_class[label].append(pred == label)
        per_incident.setdefault(incident_id, (label, []))[1].append(pred)
    recall = {label: sum(v) / len(v) for label, v in per_class.items()}
    majority = {
        inc: {"label": label, "prediction": max(set(preds), key=preds.count), "windows": len(preds)}
        for inc, (label, preds) in per_incident.items()
    }
    return {
        "windows": len(results),
        "window_accuracy": sum(p == l for _, l, p in results) / len(results) if results else None,
        "per_class_recall": recall,
        "balanced_accuracy": sum(recall.values()) / len(recall) if recall else None,
        "incidents_correct": sum(m["label"] == m["prediction"] for m in majority.values()),
        "incidents": len(majority),
        "per_incident": majority,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="also run the passive LLM baseline (OpenAI)")
    ap.add_argument("--llm-windows", type=int, default=6, help="windows sampled per incident for the LLM")
    ap.add_argument("--extra", nargs="*", default=[], help="clone incident JSON files from collect_clone_incident.py")
    ap.add_argument("--drop", nargs="*", default=[], help="metric keys to hide from the classifiers (leak ablation)")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    load_repo_dotenv(Path(__file__))
    es = HttpElasticsearchClient(os.environ["FAULTLINE_ELASTICSEARCH_URL"], api_key=os.environ.get("FAULTLINE_ELASTICSEARCH_API_KEY"))

    incidents = production_incidents(es) + extra_incidents(args.extra)
    for inc in incidents:
        inc["rows"] = ambiguity_rows(inc["windows"])  # label-free export; only `metrics` reaches a classifier
        for row in inc["rows"]:
            row["metrics"] = {k: v for k, v in row["metrics"].items() if k not in args.drop}
        inc["baseline_metrics"] = {k: v for k, v in inc["baseline"].metrics().items() if k not in args.drop}
        print(f"{inc['incident_id']:<24} {inc['origin']:<10} {inc['label']:<9} {len(inc['rows']):>3} incident windows")
    labels = {inc["label"] for inc in incidents}
    if len(labels) < 2:
        print("need both classes; got", labels)
        return 1

    # nearest centroid, leave-one-incident-out
    centroid_results = []
    for held in incidents:
        train = [(o["label"], r["metrics"]) for o in incidents if o is not held for r in o["rows"]]
        if {l for l, _ in train} != labels:
            print(f"  skip LOIO for {held['incident_id']}: training set lacks a class")
            continue
        centroids, scales = fit_centroids(train)
        centroid_results += [(held["incident_id"], held["label"], predict_centroid(r["metrics"], centroids, scales)) for r in held["rows"]]
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "incidents": [{"incident_id": i["incident_id"], "origin": i["origin"], "label": i["label"], "windows": len(i["rows"])} for i in incidents],
        "nearest_centroid_loio": score(centroid_results),
    }

    if args.llm:
        model = os.environ.get("OPENAI_MODEL", "gpt-4.1")
        oa = httpx.Client(base_url="https://api.openai.com", headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
        llm_results = []
        for inc in incidents:
            rows = inc["rows"]
            step = max(1, len(rows) // args.llm_windows)
            for row in rows[::step][: args.llm_windows]:
                pred = llm_predict(oa, model, inc["baseline_metrics"], row["metrics"])
                llm_results.append((inc["incident_id"], inc["label"], pred))
                print(f"  llm {inc['incident_id']} {row['window_start']} label={inc['label']} pred={pred}")
        report["passive_llm"] = {"model": model, **score(llm_results)}

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str))
    for name in ("nearest_centroid_loio", "passive_llm"):
        if name in report:
            s = report[name]
            print(f"{name}: window acc {s['window_accuracy']:.0%}, balanced {s['balanced_accuracy']:.0%}, "
                  f"recall {({k: round(v, 2) for k, v in s['per_class_recall'].items()})}, "
                  f"incidents {s['incidents_correct']}/{s['incidents']} by majority, {s['windows']} windows")
    print("wrote", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
