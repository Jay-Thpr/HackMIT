import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.common.stats import Stats

from . import runtime, relay, worker
from .store import CommerceStore, ConflictError, StoreUnavailable, CONN_ERRORS

log = logging.getLogger("advanced.app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

stats = Stats("advanced",
              counters=("requests", "attempts", "errors", "completed", "admissions_denied",
                        "cache_hits", "cache_misses", "duplicates"),
              hists=("fulfill", "request"))
ROLE = runtime.ROLE
TENANT = re.compile(r"^[a-z0-9-]{1,32}$")
CACHE_TTL_S = 30
INSTANCE_ID = uuid.uuid4().hex

_state: dict = {}


async def _connect_clients() -> None:
    try:
        if ROLE == "loadgen":
            _state["redis"] = aioredis.from_url(runtime.redis_url())
            from . import loadgen
            _state["task"] = asyncio.create_task(
                loadgen.run(_state["redis"], stats, _state["stop"]))
            return
        _state["store"] = CommerceStore()
        await _state["store"].connect()
        _state["redis"] = aioredis.from_url(runtime.redis_url())
        if ROLE == "worker":
            _state["consumer"] = worker.build_consumer()
            await _state["consumer"].start()
            _state["task"] = asyncio.create_task(
                worker.consume(_state["store"], _state["consumer"], _state["redis"],
                               stats, _state["stop"]))
        elif ROLE == "relay":
            _state["producer"] = relay.build_producer()
            await _state["producer"].start()
            _state["task"] = asyncio.create_task(
                relay.run(_state["store"], _state["producer"], _state["stop"]))
    except Exception as exc:
        log.error("role %s startup failed: %s", ROLE, type(exc).__name__)
        await _close_clients()
        raise


async def _close_clients() -> None:
    task = _state.get("task")
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    for key in ("consumer", "producer", "store", "redis"):
        client = _state.pop(key, None)
        if client is None:
            continue
        try:
            if key == "store":
                await client.close()
            elif key in ("consumer", "producer"):
                await client.stop()
            else:
                await client.aclose()
        except Exception:
            pass
    _state.pop("task", None)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _state["stop"] = asyncio.Event()
    await _connect_clients()
    try:
        yield
    finally:
        _state["stop"].set()
        await _close_clients()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def instrument(request, call_next):
    if ROLE != "api" or not (request.url.path.startswith("/orders")
                             or request.url.path.startswith("/catalog")):
        return await call_next(request)
    t0 = time.monotonic()
    stats.inc("requests")
    stats.inc("attempts")
    try:
        response = await call_next(request)
    except Exception:
        stats.inc("errors")
        raise
    finally:
        stats.observe("request", (time.monotonic() - t0) * 1000)
    if response.status_code >= 400:
        stats.inc("errors")
    else:
        stats.inc("completed")
    return response


class OrderBody(BaseModel):
    tenant_id: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    order_id: uuid.UUID
    amount_cents: int = Field(gt=0)


async def _admitted(tenant_id: str) -> bool:
    cap = await runtime.effective(_state["redis"], "tenant_admission", tenant_id, None)
    if not runtime.finite_positive(cap):
        return True
    key = f"admission:{tenant_id}:{int(time.time())}"
    count = await _state["redis"].incr(key)
    if count == 1:
        await _state["redis"].expire(key, 2)
    return count <= float(cap)


@app.post("/orders", status_code=202)
async def post_order(body: OrderBody):
    if not await _admitted(body.tenant_id):
        stats.inc("admissions_denied")
        raise HTTPException(status_code=429, detail="tenant admission cap reached")
    try:
        return await _state["store"].accept(body.tenant_id, body.order_id, body.amount_cents)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (StoreUnavailable, *CONN_ERRORS) as exc:
        raise HTTPException(status_code=503, detail="order store unavailable") from exc


@app.get("/orders/{tenant_id}/{order_id}")
async def get_order(tenant_id: str, order_id: uuid.UUID, strong: bool = False,
                    min_sequence: int | None = None):
    if not TENANT.fullmatch(tenant_id):
        raise HTTPException(status_code=422, detail="bad tenant_id")
    shard = runtime.shard_for(tenant_id)
    route = await runtime.effective(_state["redis"], "read_route", f"shard-{shard}", None)
    try:
        row = await _state["store"].get_order(
            tenant_id, order_id, strong=strong or route == "primary", min_sequence=min_sequence)
    except StoreUnavailable as exc:
        raise HTTPException(status_code=503, detail="order store unavailable") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="unknown order")
    row["order_id"] = str(row["order_id"])
    for key in ("accepted_at", "fulfilled_at"):
        if row.get(key) is not None:
            row[key] = row[key].isoformat()
    return row


_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""


async def _catalog_compute(tenant_id: str, item_id: int) -> dict | None:
    row = await _state["store"].catalog(tenant_id, item_id)
    if row is not None:
        ttl = await runtime.bench_override(_state["redis"], "cache_ttl", tenant_id, None)
        if not runtime.finite_positive(ttl):
            ttl = CACHE_TTL_S
        await _state["redis"].set(f"catalog:{tenant_id}:{item_id}", json.dumps(row),
                                  ex=int(ttl))
    return row


@app.get("/catalog/{tenant_id}/{item_id}")
async def get_catalog(tenant_id: str, item_id: int):
    if not TENANT.fullmatch(tenant_id):
        raise HTTPException(status_code=422, detail="bad tenant_id")
    cache_key = f"catalog:{tenant_id}:{item_id}"
    cached = await _state["redis"].get(cache_key)
    if cached is not None:
        stats.inc("cache_hits")
        return json.loads(cached)
    stats.inc("cache_misses")
    coalesce = await runtime.effective(_state["redis"], "cache_coalescing", tenant_id, None)
    try:
        if coalesce:
            token = uuid.uuid4().hex
            lock_key = f"lock:catalog:{tenant_id}:{item_id}"
            if await _state["redis"].set(lock_key, token, nx=True, px=3000):
                try:
                    row = await _catalog_compute(tenant_id, item_id)
                finally:
                    await _state["redis"].eval(_RELEASE, 1, lock_key, token)
            else:
                deadline = time.monotonic() + 3
                row = None
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                    cached = await _state["redis"].get(cache_key)
                    if cached is not None:
                        return json.loads(cached)
                row = await _state["store"].catalog(tenant_id, item_id)
        else:
            row = await _catalog_compute(tenant_id, item_id)
    except StoreUnavailable as exc:
        raise HTTPException(status_code=503, detail="catalog store unavailable") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="unknown catalog item")
    return row


@app.get("/healthz")
async def healthz():
    ok = True
    store = _state.get("store")
    if ROLE != "loadgen":
        ok = ok and store is not None and await store.healthy()
    task = _state.get("task")
    if ROLE in ("worker", "relay", "loadgen"):
        ok = ok and task is not None and not task.done()
    client = _state.get("redis")
    ok = ok and client is not None
    if ok and client is not None:
        try:
            await client.ping()
        except Exception:
            ok = False
    if not ok:
        return JSONResponse({"ok": False, "role": ROLE}, status_code=503)
    return {"ok": True, "role": ROLE}


@app.get("/stats")
async def get_stats():
    payload = stats.snapshot()
    payload["instance_id"] = INSTANCE_ID
    if ROLE == "loadgen":
        from . import loadgen
        payload["tenants"] = {t: s.snapshot() for t, s in loadgen.TENANT_STATS.items()}
    return payload
