"""Open-loop load generator: Poisson arrivals at LOAD_RPS logical checkouts/s into Envoy.

Open loop matters: arrivals do not slow down when the system does (a closed-loop client
would throttle itself and hide the storm). Rate is adjustable at runtime via POST /rate.
"""

import asyncio
import logging
import os
import random
import time
from contextlib import asynccontextmanager

import aiohttp
from fastapi import FastAPI
from pydantic import BaseModel, Field

from services.common.stats import Stats

log = logging.getLogger("loadgen")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

TARGET_URL = os.environ.get("TARGET_URL", "http://envoy:8080/checkout")
DEFAULT_RPS = float(os.environ.get("LOAD_RPS", "80"))
CLIENT_TIMEOUT_S = float(os.environ.get("LOAD_CLIENT_TIMEOUT_MS", "10000")) / 1000.0
seed = os.environ.get("LOAD_SEED")
rng = random.Random(int(seed) if seed else None)

stats = Stats("loadgen", counters=("sent", "ok", "errors", "client_timeouts_or_conn_errors"), hists=("request",))
state = {"rps": DEFAULT_RPS}
_session: aiohttp.ClientSession | None = None
_tasks: set[asyncio.Task] = set()


async def _fire() -> None:
    t0 = time.monotonic()
    stats.inc("sent")
    try:
        async with _session.post(TARGET_URL, timeout=aiohttp.ClientTimeout(total=CLIENT_TIMEOUT_S)) as r:
            await r.read()
            stats.inc("ok" if r.status == 200 else "errors")
            if r.status != 200:
                stats.inc(f"status_{r.status}")
    except (asyncio.TimeoutError, aiohttp.ClientError):
        stats.inc("errors")
        stats.inc("client_timeouts_or_conn_errors")
    stats.observe("request", (time.monotonic() - t0) * 1000)


async def _generate() -> None:
    next_t = time.monotonic()
    while True:
        rps = state["rps"]
        if rps <= 0:
            await asyncio.sleep(0.1)
            next_t = time.monotonic()
            continue
        next_t += rng.expovariate(rps)
        delay = next_t - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        elif delay < -1.0:  # fell far behind (e.g. process paused): don't burst to catch up
            next_t = time.monotonic()
        t = asyncio.create_task(_fire())
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _session
    _session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0))
    gen = asyncio.create_task(_generate())
    log.info("generating %.1f req/s against %s", state["rps"], TARGET_URL)
    yield
    gen.cancel()
    await _session.close()


app = FastAPI(lifespan=lifespan)


class RateBody(BaseModel):
    rps: float = Field(ge=0, le=2000)


@app.post("/rate")
async def set_rate(body: RateBody):
    state["rps"] = body.rps
    log.info("rate set to %.1f req/s", body.rps)
    return state


@app.delete("/rate")
async def reset_rate():
    state["rps"] = DEFAULT_RPS
    return state


@app.get("/stats")
async def get_stats():
    stats.gauges.update(target_rps=state["rps"], in_flight=len(_tasks))
    return stats.snapshot()


@app.get("/healthz")
async def healthz():
    return {"ok": True}
