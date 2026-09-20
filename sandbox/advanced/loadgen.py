import asyncio
import json
import logging
import os
import random
import time
import uuid

import aiohttp

from services.common.stats import Stats

from . import runtime

log = logging.getLogger("advanced.loadgen")

API_URL = os.environ.get("API_URL", "http://api:8080")
MAX_OUTSTANDING = 128
CATALOG_ITEMS = 32
DRAIN_TIMEOUT_S = 10
INSTANCE_ID = uuid.uuid4().hex
COUNTERS = ("requests", "attempts", "errors", "completed", "receipt_errors", "overloaded")
TENANT_STATS = {t: Stats("advanced", counters=COUNTERS, hists=("request",))
                for t in runtime.SPEC["workload"]["tenants"]}


def _order_uuid(session: str, seq: int) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"faultline-advanced/{session}/{seq}")


async def run(redis, stats, stop: asyncio.Event) -> None:
    workload = runtime.SPEC["workload"]
    rng = random.Random(workload["seed"])
    session_id = uuid.uuid4().hex[:12]
    seq = 0
    partial = False
    tasks: set[asyncio.Task] = set()
    timeout = aiohttp.ClientTimeout(total=5)

    async def issue(http: aiohttp.ClientSession, method: str, url: str,
                    body: dict | None, order_id: uuid.UUID | None, tenant: str) -> None:
        nonlocal partial
        tstats = TENANT_STATS.get(tenant)

        def inc(name):
            stats.inc(name)
            if tstats is not None:
                tstats.inc(name)

        inc("requests")
        inc("attempts")
        t0 = time.monotonic()
        try:
            async with http.request(method, url, json=body) as resp:
                payload = await resp.read()
                if method == "POST":
                    if resp.status != 202:
                        inc("errors")
                    else:
                        try:
                            accepted = json.loads(payload)
                            receipt_seq = accepted["sequence"]
                            if not isinstance(receipt_seq, int) or isinstance(receipt_seq, bool):
                                raise ValueError("no sequence")
                        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                            inc("receipt_errors")
                            inc("errors")
                            partial = True
                            stats.gauges["receipts_partial"] = 1
                            if tstats is not None:
                                tstats.gauges["receipts_partial"] = 1
                        else:
                            try:
                                await redis.rpush("receipts", json.dumps({
                                    "session": session_id,
                                    "tenant_id": tenant,
                                    "order_id": str(order_id),
                                    "sequence": receipt_seq,
                                }))
                            except Exception:
                                inc("receipt_errors")
                                partial = True
                                stats.gauges["receipts_partial"] = 1
                                if tstats is not None:
                                    tstats.gauges["receipts_partial"] = 1
                            inc("completed")
                elif resp.status < 400:
                    inc("completed")
                else:
                    inc("errors")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            inc("errors")
        finally:
            elapsed = (time.monotonic() - t0) * 1000
            stats.observe("request", elapsed)
            if tstats is not None:
                tstats.observe("request", elapsed)

    async with aiohttp.ClientSession(timeout=timeout) as http:
        base_rps = float(runtime.LOAD_RPS) if runtime.finite_positive(runtime.LOAD_RPS) \
            else float(workload["rps"])
        next_at = time.monotonic()
        while not stop.is_set():
            override = await runtime.bench_override(redis, "workload", "global", None)
            extra_rps = 0.0
            target_tenant = None
            if isinstance(override, dict):
                if runtime.finite_positive(override.get("extra_rps")):
                    extra_rps = float(override["extra_rps"])
                if isinstance(override.get("tenant"), str) and override["tenant"] in workload["tenants"]:
                    target_tenant = override["tenant"]
            total_rps = base_rps + extra_rps
            next_at += 1.0 / max(total_rps, 0.1)
            if len(tasks) >= MAX_OUTSTANDING:
                stats.inc("overloaded")
                tstats_all = TENANT_STATS.get(target_tenant) if target_tenant else None
                if tstats_all is not None:
                    tstats_all.inc("overloaded")
            else:
                seq += 1
                if target_tenant is not None and rng.random() < extra_rps / total_rps:
                    tenant = target_tenant
                else:
                    tenant = rng.choice(workload["tenants"])
                if rng.random() < workload["read_fraction"]:
                    method, url, body, order_id = "GET", \
                        f"{API_URL}/catalog/{tenant}/{rng.randint(1, CATALOG_ITEMS)}", None, None
                else:
                    order_id = _order_uuid(session_id, seq)
                    method, url = "POST", f"{API_URL}/orders"
                    body = {"tenant_id": tenant, "order_id": str(order_id), "amount_cents": 1000}
                task = asyncio.create_task(issue(http, method, url, body, order_id, tenant))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            delay = next_at - time.monotonic()
            if delay > 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=DRAIN_TIMEOUT_S)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    if partial:
        stats.gauges["receipts_partial"] = 1
