"""Backfill event detail into a comparison recording from each run's own C4 audit log.

Runs recorded before `RecordingAudit` attached `audit_detail` carry one-line titles for the
Faultline arms, while an observer arm carries its whole answer -- so the replay understates
the side that did the work. Every event already references the audit event it came from
(`reference: "c4:<event_id>"`), and each run's `audit.jsonl` is beside its telemetry, so the
detail can be restored from the run's own evidence rather than re-run or invented.

Only empty `detail` fields are filled; nothing else is touched, and a protocol note records
that the served copy was enriched. Writes a new file, leaving the original recording intact.

    uv run --no-sync python comparison_enrich.py \
        --recording pilot3-runs/cmp-<id>.json --work pilot3-runs/.work --output ../runs/comparisons
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from faultline_bench.comparison import Recording, audit_detail

NOTE = ("Event detail in this served copy was restored from each run's own audit.jsonl "
        "(matched by the c4 reference already on every event); measurements are unchanged.")


def audit_payloads(directory: Path) -> dict[str, dict]:
    path = directory / "audit.jsonl"
    if not path.exists():
        return {}
    payloads = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        payloads[event["event_id"]] = event.get("payload") or {}
    return payloads


def enrich(recording: Recording, work: Path) -> tuple[int, int]:
    filled = considered = 0
    for run in recording.runs:
        payloads = audit_payloads(work / run.id)
        for event in run.events:
            if not event.reference or not event.reference.startswith("c4:") or event.detail:
                continue
            considered += 1
            detail = audit_detail(payloads.get(event.reference[3:], {}))
            if detail:
                event.detail = detail
                filled += 1
    if filled and NOTE not in recording.protocol.notes:
        recording.protocol.notes.append(NOTE)
    return filled, considered


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True, help="directory holding <run_id>/audit.jsonl")
    parser.add_argument("--output", type=Path, required=True, help="directory to write the enriched copy into")
    args = parser.parse_args(argv)

    recording = Recording.model_validate_json(args.recording.read_text())
    filled, considered = enrich(recording, args.work)
    args.output.mkdir(parents=True, exist_ok=True)
    destination = args.output / f"{recording.id}.json"
    destination.write_text(recording.model_dump_json(indent=2))
    print(f"{filled} of {considered} referenced event(s) given detail")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
