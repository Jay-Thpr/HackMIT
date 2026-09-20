from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any

from faultline_contracts.fingerprint import Edge, Fingerprint, ResourceStats, ServiceStats, SloStatus

from .fingerprint import _quantile


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _instances(snapshot: dict) -> dict[str, dict[str, dict]]:
    groups: dict[str, dict[str, dict]] = defaultdict(dict)
    for identity, item in snapshot.get("instances", {}).items():
        if isinstance(item, dict) and isinstance(item.get("service"), str) and isinstance(item.get("stats"), dict):
            groups[item["service"]][identity] = item["stats"]
    return groups


def _service(before: dict[str, dict], after: dict[str, dict], window_s: float) -> ServiceStats | None:
    if not before or before.keys() != after.keys():
        return None
    requests = attempts = errors = timeouts = qps = 0.0
    counters = ("requests", "attempts", "errors", "attempt_timeouts")
    totals: dict[str, float | None] = {key: 0.0 for key in counters}
    buckets: list[float] | None = None
    counts: list[float] | None = None
    for identity, current in after.items():
        previous = before[identity]
        start, end = _number(previous.get("t")), _number(current.get("t"))
        if start is None or end is None or not 0 < end - start <= 2 * window_s:
            return None
        for key in counters:
            old = _number(previous.get("counters", {}).get(key))
            new = _number(current.get("counters", {}).get(key))
            if old is None or new is None:
                totals[key] = None
            elif new < old:
                return None
            elif totals[key] is not None:
                totals[key] += new - old
        old_requests = _number(previous.get("counters", {}).get("requests"))
        new_requests = _number(current.get("counters", {}).get("requests"))
        if old_requests is None or new_requests is None or new_requests < old_requests:
            return None
        qps += (new_requests - old_requests) / (end - start)
        old_hist = previous.get("hists", {}).get("request", {}).get("counts")
        new_hist = current.get("hists", {}).get("request", {}).get("counts")
        current_buckets = current.get("buckets_ms")
        if (not isinstance(old_hist, list) or not isinstance(new_hist, list)
                or not isinstance(current_buckets, list) or not current_buckets
                or previous.get("buckets_ms") != current_buckets
                or len(old_hist) != len(new_hist) or len(new_hist) != len(current_buckets) + 1
                or any(_number(value) is None for value in [*old_hist, *new_hist, *current_buckets])
                or any(a >= b for a, b in zip(current_buckets, current_buckets[1:]))):
            return None
        delta = [float(new - old) for old, new in zip(old_hist, new_hist)]
        if any(value < 0 for value in delta):
            return None
        if buckets is None:
            buckets, counts = list(current_buckets), delta
        elif buckets != current_buckets:
            return None
        else:
            counts = [a + b for a, b in zip(counts, delta)]
    requests, attempts, errors, timeouts = (totals[key] for key in counters)
    if requests is None or (errors is not None and errors > requests):
        return None
    histogram = (buckets, counts) if buckets is not None and counts is not None else None
    return ServiceStats(
        qps=qps,
        p50_ms=_quantile(histogram, 0.50),
        p99_ms=_quantile(histogram, 0.99),
        error_rate=errors / requests if errors is not None and requests > 0 else None,
        retry_ratio=attempts / requests if attempts is not None and requests > 0 else None,
        timeout_rate=timeouts / attempts if timeouts is not None and attempts is not None
        and attempts > 0 and timeouts <= attempts else None,
    )


def fingerprint_from_distributed(previous: dict, current: dict, start: datetime, end: datetime,
                                 thresholds: dict[str, float] | None = None) -> Fingerprint:
    elapsed = (end - start).total_seconds()
    if elapsed <= 0:
        raise ValueError("distributed observation boundaries must increase")
    before, after = _instances(previous), _instances(current)
    services = {}
    for name in before.keys() & after.keys():
        stats = _service(before[name], after[name], elapsed)
        if stats is not None:
            services[name] = stats
    resources = {}
    ratio_fields = {"cache_hit_ratio", "cpu_throttled_ratio"}
    for name, values in current.get("resources", {}).items():
        if not isinstance(values, dict):
            continue
        fields = {}
        for key in ResourceStats.model_fields:
            value = _number(values.get(key))
            if value is not None and (key not in ratio_fields or value <= 1):
                fields[key] = value
        if fields:
            resources[name] = ResourceStats(**fields)
    edges = [Edge(src=edge["src"], dst=edge["dst"]) for edge in current.get("edges", [])
             if isinstance(edge, dict) and isinstance(edge.get("src"), str) and isinstance(edge.get("dst"), str)]
    fp = Fingerprint(window_start=start, window_end=end, services=services, resources=resources, edges=edges)
    metrics = fp.metrics()
    for metric, threshold in (thresholds or {}).items():
        value = metrics.get(metric)
        if value is not None and _number(threshold) is not None:
            fp.slos.append(SloStatus(name=metric.replace(".", "_"), metric=metric, threshold=threshold,
                                     value=value, breached=value > threshold))
    return fp
