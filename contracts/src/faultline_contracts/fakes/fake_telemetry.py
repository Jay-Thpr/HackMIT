"""ReplayTelemetrySource: a TelemetrySource over a fixed list of Fingerprints (e.g. fixtures JSON)."""

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from ..common import WINDOW_S
from ..fingerprint import DbStats, Edge, Fingerprint, LogHighlight, ServiceStats, SloStatus


def _mean_stats(items: list, cls):
    """Field-wise mean over pydantic stats objects, skipping None values."""
    out = {}
    for field in cls.model_fields:
        if field in ("src", "dst"):
            continue
        vals = [getattr(i, field) for i in items if getattr(i, field) is not None]
        out[field] = sum(vals) / len(vals) if vals else None
    return out


def merge_fingerprints(fps: list[Fingerprint], start: datetime, end: datetime) -> Fingerprint:
    """Aggregate several fingerprints into one covering [start, end).

    Numeric stats are averaged, log counts summed, SLO breach re-evaluated on the mean value.
    """
    if not fps:
        raise ValueError("no fingerprints to merge")
    services: dict[str, list[ServiceStats]] = defaultdict(list)
    edges: dict[tuple[str, str], list[Edge]] = defaultdict(list)
    slos: dict[str, list[SloStatus]] = defaultdict(list)
    logs: dict[tuple[str, str, str], int] = defaultdict(int)
    dbs = [f.db for f in fps if f.db is not None]
    changes = []
    for f in fps:
        for name, s in f.services.items():
            services[name].append(s)
        for e in f.edges:
            edges[(e.src, e.dst)].append(e)
        for s in f.slos:
            slos[s.name].append(s)
        for lh in f.log_highlights:
            logs[(lh.service, lh.level, lh.message)] += lh.count
        changes.extend(f.change_events)

    slo_out = []
    for name, items in slos.items():
        vals = [s.value for s in items if s.value is not None]
        value = sum(vals) / len(vals) if vals else None
        breached = value > items[0].threshold if value is not None else any(s.breached for s in items)
        slo_out.append(
            SloStatus(name=name, metric=items[0].metric, threshold=items[0].threshold, value=value, breached=breached)
        )

    return Fingerprint(
        window_start=start,
        window_end=end,
        services={n: ServiceStats(**_mean_stats(v, ServiceStats)) for n, v in services.items()},
        db=DbStats(**_mean_stats(dbs, DbStats)) if dbs else None,
        edges=[Edge(src=s, dst=d, **_mean_stats(v, Edge)) for (s, d), v in edges.items()],
        slos=slo_out,
        log_highlights=[LogHighlight(service=s, level=l, message=m, count=c) for (s, l, m), c in logs.items()],
        change_events=sorted(changes, key=lambda c: c.ts),
    )


class ReplayTelemetrySource:
    """TelemetrySource over recorded fingerprints. window() merges every recorded window that
    lies inside [start, end); raises ValueError if none do."""

    def __init__(self, fingerprints: list[Fingerprint]):
        self.fingerprints = sorted(fingerprints, key=lambda f: f.window_start)

    @classmethod
    def from_json(cls, path: str | Path) -> "ReplayTelemetrySource":
        """Load a JSON list of fingerprints (or an object with a "fingerprints" list)."""
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict):
            data = data["fingerprints"]
        return cls([Fingerprint.model_validate(d) for d in data])

    @property
    def start(self) -> datetime:
        return self.fingerprints[0].window_start

    @property
    def end(self) -> datetime:
        return self.fingerprints[-1].window_end

    def window(self, start: datetime, end: datetime) -> Fingerprint:
        inside = [f for f in self.fingerprints if f.window_start >= start and f.window_end <= end]
        if not inside:
            raise ValueError(f"no recorded windows inside [{start}, {end})")
        if len(inside) == 1 and inside[0].window_start == start and inside[0].window_end == end:
            return inside[0]
        return merge_fingerprints(inside, start, end)

    def series(self, start: datetime, end: datetime, step_s: int = WINDOW_S) -> list[Fingerprint]:
        out = []
        t = start
        step = timedelta(seconds=step_s)
        while t + step <= end:
            try:
                out.append(self.window(t, t + step))
            except ValueError:
                pass  # gap in the recording
            t += step
        return out
