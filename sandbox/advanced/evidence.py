from __future__ import annotations

import math
import re
from typing import Any


_LSN = re.compile(r"^[0-9A-Fa-f]+/[0-9A-Fa-f]+$")


def replication_lag_bytes(primary_lsn: str | None, replica_lsn: str | None) -> int | None:
    if not primary_lsn or not replica_lsn or not _LSN.fullmatch(primary_lsn) or not _LSN.fullmatch(replica_lsn):
        return None
    def position(lsn: str) -> int:
        upper, lower = lsn.split("/")
        return (int(upper, 16) << 32) + int(lower, 16)
    lag = position(primary_lsn) - position(replica_lsn)
    return lag if lag >= 0 else None


def consumer_lag(end_offset: int | None, committed_offset: int | None,
                 beginning_offset: int | None = None) -> int | None:
    if (type(end_offset) is int and type(beginning_offset) is int
            and end_offset == beginning_offset == 0 and committed_offset is None):
        return 0
    if (not isinstance(end_offset, int) or isinstance(end_offset, bool)
            or not isinstance(committed_offset, int) or isinstance(committed_offset, bool)
            or committed_offset < 0 or end_offset < committed_offset):
        return None
    return end_offset - committed_offset


def correctness(snapshot: dict[str, Any], expected_tenants: list[str]) -> dict[str, Any]:
    failures: list[str] = []
    unknown: list[str] = []
    shards = snapshot.get("shards")
    if not isinstance(shards, list) or len(shards) != 3:
        return {"status": "unknown", "failures": [], "unknown": ["all three shard snapshots are required"]}
    seen_shards: set[int] = set()
    tenants: dict[str, dict] = {}
    for shard in shards:
        shard_id = shard.get("shard_id")
        if not isinstance(shard_id, int) or shard_id not in range(3) or shard_id in seen_shards:
            unknown.append("invalid or repeated shard identity")
            continue
        seen_shards.add(shard_id)
        if shard.get("complete") is not True:
            unknown.append(f"shard-{shard_id}: incomplete database observations")
            continue
        for check in ("ordering_violations", "missing_outbox", "orphan_payments", "duplicate_effects"):
            rows = shard.get(check)
            if not isinstance(rows, list):
                unknown.append(f"shard-{shard_id}: missing {check}")
            elif rows:
                failures.append(f"shard-{shard_id}: {check}")
        rows = shard.get("tenants")
        if not isinstance(rows, list):
            unknown.append(f"shard-{shard_id}: missing tenant accounting")
            continue
        for row in rows:
            tenant = row.get("tenant_id")
            if not isinstance(tenant, str) or tenant in tenants:
                unknown.append("invalid or multiply-routed tenant identity")
                continue
            tenants[tenant] = row
    for tenant in expected_tenants:
        row = tenants.get(tenant)
        if row is None:
            unknown.append(f"{tenant}: no accepted-work evidence")
            continue
        fields = ("accepted", "paid", "fulfilled", "outbox_pending", "outstanding",
                  "mismatched_effects", "inconsistent_fulfillment")
        if any(not isinstance(row.get(field), int) or isinstance(row.get(field), bool) or row[field] < 0
               for field in fields):
            unknown.append(f"{tenant}: incomplete accounting counters")
            continue
        if row["accepted"] == 0:
            unknown.append(f"{tenant}: no accepted work exercised")
        if not row["accepted"] == row["paid"] == row["fulfilled"]:
            failures.append(f"{tenant}: accepted work has not all completed")
        if any(row[field] != 0 for field in fields[3:]):
            failures.append(f"{tenant}: outstanding, unpublished, or inconsistent work remains")
    return {"status": "failed" if failures else ("unknown" if unknown else "passed"),
            "failures": failures, "unknown": unknown}


def finite_observation(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)
