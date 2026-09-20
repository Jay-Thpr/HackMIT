import asyncio
import ipaddress
import json
from datetime import datetime, timezone
from typing import Any

from . import evidence, runtime, sql

SCHEMA = "faultline-distributed-stats/1"
STATS_PORT = 8000
ROLE_SERVICE = {"loadgen": "gateway", "api": "api", "worker": "fulfillment", "relay": "relay"}
MANAGED_SELECTOR = "faultline.dev/managed-by=distributed-demo"


def _role(pod_name: str) -> str | None:
    for role in ("worker", "loadgen", "api", "relay"):
        if pod_name.startswith(role + "-") or pod_name == role:
            return role
    return None


async def _instances(kube, http, errors: list) -> dict[str, dict]:
    try:
        pods = await asyncio.to_thread(kube.list_pods, MANAGED_SELECTOR)
    except Exception:
        errors.append("kube pods unavailable")
        return {}
    instances: dict[str, dict] = {}
    items = pods.get("items", []) if isinstance(pods, dict) else []
    for pod in items:
        meta = pod.get("metadata", {})
        status = pod.get("status", {})
        role = _role(meta.get("name", ""))
        ip = status.get("podIP", "")
        if role is None or not ip:
            continue
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not addr.is_private or addr.is_loopback or addr.is_link_local:
            continue
        try:
            stats = await asyncio.to_thread(http, f"http://{ip}:{STATS_PORT}/stats", 2)
        except Exception:
            errors.append(f"{meta.get('name', 'pod')} /stats unavailable")
            continue
        identity = f"{meta.get('uid', meta.get('name'))}-{stats.get('instance_id', '')}".rstrip("-")
        instances[identity] = {"service": ROLE_SERVICE[role], "stats": stats}
        for tenant, tstats in (stats.get("tenants") or {}).items():
            t_identity = f"{identity}-tenant-{tenant}"
            instances[t_identity] = {
                "service": f"tenant_{tenant.replace('-', '_')}", "stats": tstats}
    return instances


async def _postgres(store, errors: list) -> tuple[dict, dict]:
    resources: dict[str, Any] = {}
    shards = []
    for shard in range(runtime.SPEC["postgres"]["shards"]):
        entry = {"shard_id": shard, "complete": False, "tenants": [],
                 "ordering_violations": [], "missing_outbox": [], "orphan_payments": [],
                 "duplicate_effects": []}
        try:
            async with store.replicas[shard].acquire(timeout=5) as conn:
                replica_pos = await conn.fetchrow(sql.REPLICA_POSITION, timeout=2)
            async with store.primaries[shard].acquire(timeout=5) as conn:
                async with conn.transaction(readonly=True, isolation="repeatable_read"):
                    primary_pos = await conn.fetchrow(sql.PRIMARY_POSITION, timeout=2)
                    peers = await conn.fetch(sql.REPLICATION_PEERS, timeout=2)
                    tenants = await conn.fetch(sql.TENANT_SNAPSHOT, timeout=2)
                    ordering = await conn.fetch(sql.ORDERING_VIOLATIONS, timeout=2)
                    missing = await conn.fetch(sql.MISSING_OUTBOX, timeout=2)
                    orphans = await conn.fetch(sql.ORPHAN_PAYMENTS, timeout=2)
                    dupes = await conn.fetch(sql.DUPLICATE_EFFECTS, timeout=2)
        except Exception:
            errors.append(f"shard {shard} snapshot unavailable")
            shards.append(entry)
            continue
        lag = evidence.replication_lag_bytes(
            primary_pos["lsn"] if primary_pos else None,
            replica_pos["lsn"] if replica_pos else None)
        if lag is not None:
            resources[f"shard_{shard}_replica"] = {"replication_lag_bytes": lag}
        entry["complete"] = True
        entry["tenants"] = [dict(r) for r in tenants]
        entry["ordering_violations"] = [dict(r) for r in ordering]
        entry["missing_outbox"] = [dict(r) for r in missing]
        entry["orphan_payments"] = [dict(r) for r in orphans]
        entry["duplicate_effects"] = [dict(r) for r in dupes]
        for row in tenants:
            name = f"tenant_{row['tenant_id'].replace('-', '_')}"
            fields = {
                "accepted_total": row["accepted"],
                "completed_total": row["fulfilled"],
                "outstanding": row["outstanding"],
                "oldest_pending_ms": row["oldest_outstanding_ms"],
            }
            resources[name] = {k: v for k, v in fields.items() if v is not None}
        shards.append(entry)
    return resources, shards


async def _kafka(consumer, errors: list) -> dict[str, dict]:
    resources: dict[str, Any] = {}
    if consumer is None:
        return resources
    topic = runtime.SPEC["kafka"]["topic"]
    try:
        partitions = consumer.partitions_for_topic(topic)
    except Exception:
        errors.append("kafka metadata unavailable")
        return resources
    if not partitions:
        return resources
    from aiokafka import TopicPartition
    for partition in sorted(partitions):
        tp = TopicPartition(topic, partition)
        try:
            end = (await consumer.end_offsets([tp]))[tp]
            committed = await consumer.committed(tp)
            beginning = (await consumer.beginning_offsets([tp]))[tp]
        except Exception:
            errors.append(f"kafka partition {partition} offsets unavailable")
            continue
        lag = evidence.consumer_lag(end, committed, beginning_offset=beginning)
        if lag is not None:
            resources[f"kafka_partition_{partition}"] = {"lag_messages": lag}
    return resources


async def _deployments(kube, errors: list) -> dict[str, dict]:
    resources: dict[str, Any] = {}
    for index in range(runtime.SPEC["replicas"]["worker"]):
        name = f"worker-{index}"
        try:
            deployment = await asyncio.to_thread(kube.get_deployment, name)
        except Exception:
            errors.append(f"deployment {name} unavailable")
            continue
        spec = deployment.get("spec", {})
        status = deployment.get("status", {})
        fields = {}
        if status.get("readyReplicas") is not None:
            fields["ready_replicas"] = status["readyReplicas"]
        if spec.get("replicas") is not None:
            fields["desired_replicas"] = spec["replicas"]
        if fields:
            resources[f"worker_{index}"] = fields
    return resources


_EDGES = (
    ("gateway", "api"),
    ("api", "shard_0_primary"), ("api", "shard_1_primary"), ("api", "shard_2_primary"),
    ("api", "shard_0_replica"), ("api", "shard_1_replica"), ("api", "shard_2_replica"),
    ("api", "redis"),
    ("relay", "kafka"),
    ("kafka", "fulfillment"),
    ("fulfillment", "shard_0_primary"), ("fulfillment", "shard_1_primary"),
    ("fulfillment", "shard_2_primary"),
    ("shard_0_primary", "shard_0_replica"), ("shard_1_primary", "shard_1_replica"),
    ("shard_2_primary", "shard_2_replica"),
)


async def snapshot(store, redis, kube, consumer, http) -> dict:
    errors: list = []
    resources: dict[str, Any] = {}
    business = {"shards": []}
    if kube is not None and http is not None:
        instances = await _instances(kube, http, errors)
    else:
        instances = {}
        errors.append("instance discovery unavailable")
    if store is not None:
        pg_resources, shards = await _postgres(store, errors)
        resources.update(pg_resources)
        business = {"shards": shards}
    else:
        errors.append("postgres store unavailable")
    if consumer is not None:
        resources.update(await _kafka(consumer, errors))
    else:
        errors.append("kafka observer unavailable")
    if kube is not None:
        resources.update(await _deployments(kube, errors))
    provenance = {}
    if kube is not None:
        provenance = {"namespace": runtime.NAMESPACE,
                      "images": {"app": runtime.SPEC["images"]["app"]}}
    payload = {
        "schema_version": SCHEMA,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "instances": instances,
        "resources": resources,
        "edges": [{"src": src, "dst": dst} for src, dst in _EDGES],
        "errors": errors,
        "business_snapshot": business,
        "provenance": provenance,
    }
    if any((inst["stats"].get("gauges") or {}).get("receipts_partial")
           for inst in instances.values()):
        payload["errors"].append("receipt ledger partial")
    return payload
