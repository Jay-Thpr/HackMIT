"""Orders: POST /checkout calls Payments (through Envoy) with a per-attempt timeout and
retries immediately on timeout/error, up to max_retries times (default 3 -> 4 attempts).

This retry loop is the code a durable fix (backoff + jitter, retry budget, circuit breaker)
would patch. The retry_cap lever sets a runtime override of max_retries with a ttl; Orders
drops the override by itself when the ttl expires.
"""

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.common.stats import RateLimitedLog, Stats, require_token

log = logging.getLogger("orders")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
rlog = RateLimitedLog(log)

VERSION = os.environ.get("SERVICE_VERSION", "v1")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://envoy:8081/pay")
DEFAULT_MAX_RETRIES = int(os.environ.get("ORDERS_MAX_RETRIES", "3"))
ATTEMPT_TIMEOUT_S = float(os.environ.get("ORDERS_ATTEMPT_TIMEOUT_MS", "500")) / 1000.0

stats = Stats("orders")
_override: dict[str, Any] = {"max_retries": None, "expires": 0.0}
_session: aiohttp.ClientSession | None = None
_in_flight = 0


def max_retries() -> int:
    if _override["max_retries"] is not None:
        if time.monotonic() < _override["expires"]:
            return _override["max_retries"]
        log.info("retry override ttl expired, max_retries back to %d", DEFAULT_MAX_RETRIES)
        _override.update(max_retries=None, expires=0.0)
    return DEFAULT_MAX_RETRIES


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _session
    _session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0, ttl_dns_cache=10))
    yield
    await _session.close()


app = FastAPI(lifespan=lifespan)


async def _attempt(order_id: str) -> tuple[bool, str]:
    t0 = time.monotonic()
    try:
        async with _session.post(PAYMENTS_URL, json={"order_id": order_id, "amount_cents": 1000},
                                 timeout=aiohttp.ClientTimeout(total=ATTEMPT_TIMEOUT_S)) as r:
            await r.read()
            ok = r.status == 200
            outcome = "ok" if ok else f"status {r.status}"
    except asyncio.TimeoutError:
        ok, outcome = False, "timeout"
    except aiohttp.ClientError as e:
        ok, outcome = False, f"error {type(e).__name__}"
    stats.observe("attempt", (time.monotonic() - t0) * 1000)
    return ok, outcome


@app.post("/checkout")
async def checkout():
    global _in_flight
    t0 = time.monotonic()
    order_id = uuid.uuid4().hex
    stats.inc("requests")
    _in_flight += 1
    try:
        attempt = 0
        while True:
            attempt += 1
            stats.inc("attempts")
            if attempt > 1:
                stats.inc("retries")
            ok, outcome = await _attempt(order_id)
            if ok:
                stats.inc("ok")
                return {"order_id": order_id, "attempts": attempt, "version": VERSION}
            limit = max_retries() + 1  # re-read each time: a new cap applies to in-flight requests
            if outcome == "timeout":
                stats.inc("attempt_timeouts")
                rlog.log(logging.WARNING, "timeout", "payments call timed out after %dms, retrying (attempt %d of %d)",
                         ATTEMPT_TIMEOUT_S * 1000, attempt, limit)
            else:
                stats.inc("attempt_errors")
                rlog.log(logging.WARNING, "error", "payments call failed: %s (attempt %d of %d)",
                         outcome, attempt, limit)
            if attempt >= limit:
                stats.inc("errors")
                rlog.log(logging.ERROR, "failed", "checkout failed: payments unavailable after %d attempts", attempt)
                return JSONResponse({"error": "payments unavailable", "attempts": attempt}, status_code=503)
    finally:
        _in_flight -= 1
        stats.observe("request", (time.monotonic() - t0) * 1000)


@app.get("/stats")
async def get_stats():
    stats.gauges.update(max_retries=max_retries(), attempt_timeout_ms=ATTEMPT_TIMEOUT_S * 1000,
                        in_flight=_in_flight, version=VERSION)
    return stats.snapshot()


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": VERSION}


class OverrideBody(BaseModel):
    max_retries: int = Field(ge=0, le=10)
    ttl_s: float = Field(gt=0)


def _override_state() -> dict[str, Any]:
    eff = max_retries()
    remaining = max(0.0, _override["expires"] - time.monotonic()) if _override["max_retries"] is not None else 0.0
    return {"max_retries": eff, "override": _override["max_retries"], "remaining_s": round(remaining, 3)}


@app.post("/internal/retry_override", dependencies=[Depends(require_token)])
async def set_override(body: OverrideBody):
    _override.update(max_retries=body.max_retries, expires=time.monotonic() + body.ttl_s)
    log.info("retry override set: max_retries=%d for %.0fs", body.max_retries, body.ttl_s)
    return _override_state()


@app.delete("/internal/retry_override", dependencies=[Depends(require_token)])
async def clear_override():
    _override.update(max_retries=None, expires=0.0)
    return _override_state()


@app.get("/internal/retry_override", dependencies=[Depends(require_token)])
async def get_override():
    return _override_state()
