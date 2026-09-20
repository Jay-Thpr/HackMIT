"""Payments: POST /pay runs one DB query through a small connection pool.

Capacity model: the pool has DB_POOL_SIZE connections and each query holds one for its
service time S (set inside the DB), so the DB path serves at most mu = DB_POOL_SIZE / S
queries/s. Queries beyond that wait FIFO for a connection, up to DB_ACQUIRE_TIMEOUT_MS.

Invariant the retry storm depends on: the DB work of a request is NOT cancelled when the
caller gives up (Orders times out, Envoy resets the stream). It runs in its own task
under asyncio.shield, so an abandoned attempt still occupies the pool and the DB.

db_failover lever: /internal/db_target switches new queries to the standby pool, with a
ttl after which Payments reverts to primary by itself.
"""

import asyncio
import hashlib
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.common.stats import RateLimitedLog, Stats, require_token
from services.common.telemetry import configure_application_logs

log = logging.getLogger("payments")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
configure_application_logs(log)
rlog = RateLimitedLog(log)

POOLS_CFG = {
    "primary": (os.environ.get("PRIMARY_DSN", "postgresql://app@db-primary:5432/shop"),
                int(os.environ.get("DB_POOL_SIZE", "4"))),
    "standby": (os.environ.get("STANDBY_DSN", "postgresql://app@db-standby:5432/shop"),
                int(os.environ.get("STANDBY_POOL_SIZE", "16"))),
}
ACQUIRE_TIMEOUT_S = float(os.environ.get("DB_ACQUIRE_TIMEOUT_MS", "1000")) / 1000.0
CPU_WORK_S = float(os.environ.get("PAYMENTS_CPU_WORK_MS", "1")) / 1000.0

stats = Stats("payments", counters=("requests", "errors", "db_queries_issued", "db_queries_completed", "db_errors",
                                    "db_acquire_timeouts", "db_busy_s", "completed_after_client_gone"),
              hists=("request", "db_query"))
pools: dict[str, asyncpg.Pool] = {}
in_use: dict[str, int] = {k: 0 for k in POOLS_CFG}
waiting: dict[str, int] = {k: 0 for k in POOLS_CFG}
_target = {"name": "primary", "expires": 0.0}  # standby only while now < expires
_background: set[asyncio.Task] = set()


def current_target() -> str:
    if _target["name"] != "primary" and time.monotonic() >= _target["expires"]:
        log.info("db target ttl expired, reverting to primary")
        _target.update(name="primary", expires=0.0)
    return _target["name"]


async def _connect_pools() -> None:
    for name, (dsn, size) in POOLS_CFG.items():
        while True:
            try:
                pools[name] = await asyncpg.create_pool(dsn, min_size=size, max_size=size, command_timeout=60)
                log.info("pool %s ready (size=%d)", name, size)
                break
            except (OSError, asyncpg.PostgresError):
                log.warning("pool %s not ready; retrying", name)
                await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await _connect_pools()
    yield
    for p in pools.values():
        await p.close()


app = FastAPI(lifespan=lifespan)


async def _run_query(order_id: str, amount_cents: int) -> tuple[int, dict[str, Any]]:
    """Acquire a connection (FIFO wait) and run the payment query. Never cancelled by the caller."""
    target = current_target()
    pool = pools[target]
    t0 = time.monotonic()
    stats.inc("db_queries_issued")
    waiting[target] += 1
    try:
        conn = await pool.acquire(timeout=ACQUIRE_TIMEOUT_S)
    except asyncio.TimeoutError:
        waited_ms = (time.monotonic() - t0) * 1000
        stats.inc("db_errors")
        stats.inc("db_acquire_timeouts")
        stats.observe("db_query", waited_ms)
        rlog.log(logging.WARNING, "pool", "db connection pool exhausted, waited %dms for a connection", waited_ms)
        return 503, {"error": "db connection pool exhausted"}
    except (OSError, asyncpg.PostgresError):
        stats.inc("db_errors")
        rlog.log(logging.ERROR, "conn", "db connection error")
        return 503, {"error": "db unavailable"}
    finally:
        waiting[target] -= 1

    in_use[target] += 1
    t1 = time.monotonic()
    try:
        payment_id = await conn.fetchval("SELECT process_payment($1, $2)", order_id, amount_cents)
    except (OSError, asyncpg.PostgresError):
        stats.inc("db_errors")
        rlog.log(logging.ERROR, "query", "db query failed")
        return 503, {"error": "db query failed"}
    finally:
        t2 = time.monotonic()
        in_use[target] -= 1
        stats.inc("db_busy_s", t2 - t1)
        await pool.release(conn)
    # client-side query latency: from issuing the query to having the result (includes pool wait)
    total_ms = (t2 - t0) * 1000
    stats.observe("db_query", total_ms)
    stats.inc("db_queries_completed")
    if total_ms > 1000:
        rlog.log(logging.WARNING, "slow", "slow database operation took %dms", total_ms)
    return 200, {"payment_id": payment_id}


def _risk_score(order_id: str) -> int:
    """Synchronous CPU work per payment (fraud scoring stand-in), measured in CPU time so a
    CPU-starved container takes proportionally longer in wall time."""
    end, h = time.thread_time() + CPU_WORK_S, hashlib.sha256(order_id.encode())
    while time.thread_time() < end:
        for _ in range(50):
            h = hashlib.sha256(h.digest())
    return h.digest()[0]


class PayBody(BaseModel):
    order_id: str = "unknown"
    amount_cents: int = 1000


@app.post("/pay")
async def pay(body: PayBody, request: Request):
    t0 = time.monotonic()
    stats.inc("requests")
    _risk_score(body.order_id)
    task = asyncio.create_task(_run_query(body.order_id, body.amount_cents))
    _background.add(task)
    task.add_done_callback(_background.discard)
    # shield: if this handler is cancelled (client disconnect), the query task keeps running
    status, payload = await asyncio.shield(task)
    stats.observe("request", (time.monotonic() - t0) * 1000)
    if status != 200:
        stats.inc("errors")
    if await request.is_disconnected():
        stats.inc("completed_after_client_gone")
    return JSONResponse(payload, status_code=status)


@app.get("/stats")
async def get_stats():
    current_target()
    for name, (_dsn, size) in POOLS_CFG.items():
        stats.gauges[f"pool_{name}_size"] = size
        stats.gauges[f"pool_{name}_in_use"] = in_use[name]
        stats.gauges[f"pool_{name}_waiting"] = waiting[name]
    active = _target["name"]
    stats.gauges.update(db_target=active, pool_size=POOLS_CFG[active][1], pool_in_use=sum(in_use.values()),
                        pool_waiting=sum(waiting.values()), background_tasks=len(_background))
    return stats.snapshot()


@app.get("/healthz")
async def healthz():
    return {"ok": bool(pools)}


class TargetBody(BaseModel):
    target: str = Field(pattern="^(primary|standby)$")
    ttl_s: float = Field(gt=0)


def _target_state() -> dict[str, Any]:
    name = current_target()
    remaining = max(0.0, _target["expires"] - time.monotonic()) if name != "primary" else 0.0
    return {"target": name, "remaining_s": round(remaining, 3)}


@app.post("/internal/db_target", dependencies=[Depends(require_token)])
async def set_target(body: TargetBody):
    _target.update(name=body.target, expires=time.monotonic() + body.ttl_s)
    log.info("db target set to %s for %.0fs", body.target, body.ttl_s)
    return _target_state()


@app.delete("/internal/db_target", dependencies=[Depends(require_token)])
async def clear_target():
    _target.update(name="primary", expires=0.0)
    return _target_state()


@app.get("/internal/db_target", dependencies=[Depends(require_token)])
async def get_target():
    return _target_state()
