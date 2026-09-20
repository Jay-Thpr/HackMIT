"""Read-only analysis and presentation for frozen or live benchmark JSON."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from random import Random
from typing import Any


NO_FAULT_LABELS = {"no_fault", "no_incident", "none_of_the_above"}


def _result(run: dict[str, Any]) -> dict[str, Any]:
    return run.get("result") or {}


def _correct(run: dict[str, Any]) -> bool:
    return bool(run.get("correct", _result(run).get("diagnosis") == run.get("expected_diagnosis")))


def _rate(runs: list[dict[str, Any]], predicate) -> float | None:
    return sum(bool(predicate(run)) for run in runs) / len(runs) if runs else None


def _mean(runs: list[dict[str, Any]], field: str) -> float | None:
    values = [_result(run).get(field) for run in runs if isinstance(_result(run).get(field), (int, float))]
    return sum(values) / len(values) if values else None


def _bootstrap(runs: list[dict[str, Any]], samples: int, seed: int) -> list[float] | None:
    if not runs:
        return None
    rng = Random(seed)
    values = []
    for _ in range(samples):
        drawn = [runs[rng.randrange(len(runs))] for _ in runs]
        values.append(sum(_correct(run) for run in drawn) / len(drawn))
    return sorted(values)


def _ci(values: list[float] | None) -> list[float] | None:
    if not values:
        return None
    return [values[int((len(values) - 1) * 0.025)], values[int((len(values) - 1) * 0.975)]]


def analyze(payload: dict[str, Any], *, bootstrap_samples: int = 1_000, seed: int = 7) -> dict[str, Any]:
    """Calculate transparent aggregate metrics from ``SuiteReport.to_dict`` output.

    Missing optional live fields (tokens, cost, request impact) remain ``null``;
    the report never invents a value for an arm that did not collect one.
    """
    runs = list(payload.get("runs") or [])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_world: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[str(run.get("arm", "unknown"))].append(run)
        by_world[(str(run.get("arm", "unknown")), str(run.get("scenario", "unknown")))].append(run)

    arms = []
    for arm, arm_runs in sorted(grouped.items()):
        worlds = {
            world: _rate(items, _correct)
            for (candidate_arm, world), items in sorted(by_world.items())
            if candidate_arm == arm
        }
        balanced = sum(worlds.values()) / len(worlds) if worlds else None
        false_confirmed = _rate(arm_runs, lambda run: bool(_result(run).get("confirmed")) and not _correct(run))
        no_fault = [run for run in arm_runs if str(run.get("expected_diagnosis")) in NO_FAULT_LABELS]
        arms.append({
            "arm": arm,
            "runs": len(arm_runs),
            "accuracy_by_world": worlds,
            "balanced_accuracy": balanced,
            "balanced_accuracy_ci95": _ci(_bootstrap(arm_runs, bootstrap_samples, seed)),
            "false_confirmation_rate": false_confirmed,
            "no_fault_false_positive_rate": _rate(no_fault, lambda run: _result(run).get("diagnosis") not in NO_FAULT_LABELS) if no_fault else None,
            "mean_time_to_verdict_s": _mean(arm_runs, "time_to_verdict_seconds"),
            "mean_actions": _mean(arm_runs, "actions_applied"),
            "mean_requests_affected": _mean(arm_runs, "requests_affected"),
            "mean_tokens": _mean(arm_runs, "token_usage"),
            "mean_estimated_cost_usd": _mean(arm_runs, "estimated_cost_usd"),
        })
    return {"runs": len(runs), "bootstrap_samples": bootstrap_samples, "arms": arms}


def markdown(analysis: dict[str, Any]) -> str:
    lines = ["# Faultline benchmark analysis", "", "| Arm | Balanced accuracy (95% CI) | False confirmations | No-fault false positives | Time to verdict | Actions | Requests affected | Tokens / cost |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    def fmt(value, suffix=""):
        return "—" if value is None else f"{value:.3f}{suffix}"
    for item in analysis["arms"]:
        ci = item["balanced_accuracy_ci95"]
        accuracy = fmt(item["balanced_accuracy"]) + (f" ({ci[0]:.3f}–{ci[1]:.3f})" if ci else "")
        tokens = "—" if item["mean_tokens"] is None else f"{item['mean_tokens']:.0f} / ${item['mean_estimated_cost_usd']:.4f}" if item["mean_estimated_cost_usd"] is not None else f"{item['mean_tokens']:.0f} / —"
        lines.append(f"| {item['arm']} | {accuracy} | {fmt(item['false_confirmation_rate'])} | {fmt(item['no_fault_false_positive_rate'])} | {fmt(item['mean_time_to_verdict_s'], ' s')} | {fmt(item['mean_actions'])} | {fmt(item['mean_requests_affected'])} | {tokens} |")
        worlds = ", ".join(f"{world}: {score:.3f}" for world, score in item["accuracy_by_world"].items())
        lines.append(f"| ↳ by world | {worlds or '—'} |  |  |  |  |  |  |")
    return "\n".join(lines) + "\n"


def svg(analysis: dict[str, Any]) -> str:
    width, row = 720, 42
    height = 70 + row * len(analysis["arms"])
    bars = []
    for index, item in enumerate(analysis["arms"]):
        y = 48 + row * index
        value = item["balanced_accuracy"] or 0
        bars.append(f'<text x="12" y="{y + 16}" fill="#e5e7eb" font-family="system-ui" font-size="14">{item["arm"]}</text><rect x="180" y="{y}" width="480" height="22" rx="4" fill="#273244"/><rect x="180" y="{y}" width="{480 * value:.1f}" height="22" rx="4" fill="#34d399"/><text x="670" y="{y + 16}" fill="#e5e7eb" font-family="system-ui" font-size="13">{value:.1%}</text>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Balanced accuracy by benchmark arm"><rect width="100%" height="100%" fill="#111827"/><text x="12" y="26" fill="#fff" font-family="system-ui" font-size="18" font-weight="600">Balanced accuracy by arm</text>{"".join(bars)}</svg>\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize Faultline benchmark JSON with confidence intervals.")
    parser.add_argument("input", type=Path, help="SuiteReport JSON emitted by the benchmark runner")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--svg-out", type=Path)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    args = parser.parse_args(argv)
    analysis = analyze(json.loads(args.input.read_text()), bootstrap_samples=args.bootstrap_samples)
    rendered = markdown(analysis)
    print(rendered, end="")
    if args.json_out: args.json_out.write_text(json.dumps(analysis, indent=2) + "\n")
    if args.markdown_out: args.markdown_out.write_text(rendered)
    if args.svg_out: args.svg_out.write_text(svg(analysis))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
