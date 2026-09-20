"""Orders: POST /checkout calls Payments (through Envoy) with a per-attempt timeout and a
bounded retry policy:

* retries are capped at min(max_retries, ORDERS_RETRY_HARD_CAP) per request;
* every retry waits an exponential backoff with full jitter (ORDERS_BACKOFF_BASE_MS,
  ORDERS_BACKOFF_MAX_MS) so failed attempts are not replayed as a synchronous burst;
* the whole request runs under ORDERS_REQUEST_DEADLINE_MS: a retry is only started if it can
  still finish before the deadline, and its timeout is clipped to the time left, so a
  checkout never takes longer than the deadline however many attempts it makes;
* a process-wide retry budget (token bucket refilled by first attempts at
  ORDERS_RETRY_BUDGET_RATIO tokens each) bounds retry traffic to a fraction of real traffic,
  so a slow Payments/DB cannot amplify load into a self-sustaining retry storm.

The retry_cap lever sets a runtime override of max_retries with a ttl; Orders drops the
override by itself when the ttl expires.
"""

import asyncio
import logging
import os
import random
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
RETRY_HARD_CAP = int(os.environ.get("ORDERS_RETRY_HARD_CAP", "2"))
BACKOFF_BASE_S = float(os.environ.get("ORDERS_BACKOFF_BASE_MS", "25")) / 1000.0
BACKOFF_MAX_S = float(os.environ.get("ORDERS_BACKOFF_MAX_MS", "200")) / 1000.0
REQUEST_DEADLINE_S = float(os.environ.get("ORDERS_REQUEST_DEADLINE_MS", "900")) / 1000.0
MIN_ATTEMPT_S = float(os.environ.get("ORDERS_MIN_ATTEMPT_MS", "100")) / 1000.0
RETRY_BUDGET_RATIO = float(os.environ.get("ORDERS_RETRY_BUDGET_RATIO", "0.2"))
RETRY_BUDGET_MAX = float(os.environ.get("ORDERS_RETRY_BUDGET_MAX", "20"))

stats = Stats("orders", counters=("requests", "attempts", "retries", "ok", "errors", "attempt_timeouts", "attempt_errors",
                                  "retries_budget_denied", "retries_deadline_denied"),
              hists=("request", "attempt", "backoff"))
_override: dict[str, Any] = {"max_retries": None, "timeout_s": None, "expires": 0.0}
_session: aiohttp.ClientSession | None = None
_in_flight = 0


class RetryBudget:
    """Token bucket: each first attempt deposits `ratio` tokens, each retry spends one.
    Retries are therefore bounded to about `ratio` x the real request rate."""

    def __init__(self, ratio: float, cap: float) -> None:
        self.ratio, self.cap, self.tokens = ratio, cap, cap

    def deposit(self) -> None:
        self.tokens = min(self.cap, self.tokens + self.ratio)

    def try_spend(self) -> bool:
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


budget = RetryBudget(RETRY_BUDGET_RATIO, RETRY_BUDGET_MAX)


def effective_max_retries() -> int:
    return min(max_retries(), RETRY_HARD_CAP)


def backoff_s(retry_no: int) -> float:
    """Exponential backoff with full jitter: U(0, min(max, base * 2**(retry_no-1)))."""
    return random.uniform(0.0, min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** (retry_no - 1))))


def _override_live() -> bool:
    if _override["max_retries"] is None and _override["timeout_s"] is None:
        return False
    if time.monotonic() < _override["expires"]:
        return True
    log.info("retry override ttl expired, max_retries back to %d", DEFAULT_MAX_RETRIES)
    _override.update(max_retries=None, timeout_s=None, expires=0.0)
    return False


def max_retries() -> int:
    if _override_live() and _override["max_retries"] is not None:
        return _override["max_retries"]
    return DEFAULT_MAX_RETRIES


def attempt_timeout_s() -> float:
    if _override_live() and _override["timeout_s"] is not None:
        return _override["timeout_s"]
    return ATTEMPT_TIMEOUT_S


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _session
    _session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0, ttl_dns_cache=10))
    yield
    await _session.close()


app = FastAPI(lifespan=lifespan)


async def _attempt(order_id: str, timeout_s: float) -> tuple[bool, str]:
    t0 = time.monotonic()
    try:
        async with _session.post(PAYMENTS_URL, json={"order_id": order_id, "amount_cents": 1000},
                                 timeout=aiohttp.ClientTimeout(total=timeout_s)) as r:
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
    budget.deposit()
    _in_flight += 1
    deadline = t0 + REQUEST_DEADLINE_S
    try:
        attempt = 0
        while True:
            attempt += 1
            stats.inc("attempts")
            if attempt > 1:
                stats.inc("retries")
            timeout_s = attempt_timeout_s() if attempt == 1 else min(attempt_timeout_s(), deadline - time.monotonic())
            ok, outcome = await _attempt(order_id, timeout_s)
            if ok:
                stats.inc("ok")
                return {"order_id": order_id, "attempts": attempt, "version": VERSION}
            limit = effective_max_retries() + 1  # re-read each time: a new cap applies to in-flight requests
            if outcome == "timeout":
                stats.inc("attempt_timeouts")
                rlog.log(logging.WARNING, "timeout", "payments call timed out after %dms (attempt %d of %d)",
                         timeout_s * 1000, attempt, limit)
            else:
                stats.inc("attempt_errors")
                rlog.log(logging.WARNING, "error", "payments call failed: %s (attempt %d of %d)",
                         outcome, attempt, limit)
            if attempt >= limit:
                stats.inc("errors")
                rlog.log(logging.ERROR, "failed", "checkout failed: payments unavailable after %d attempts", attempt)
                return JSONResponse({"error": "payments unavailable", "attempts": attempt}, status_code=503)
            delay = backoff_s(attempt)
            if time.monotonic() + delay + MIN_ATTEMPT_S > deadline:
                stats.inc("errors")
                stats.inc("retries_deadline_denied")
                rlog.log(logging.ERROR, "deadline", "checkout failed: no time left to retry after %d attempt(s)", attempt)
                return JSONResponse({"error": "payments unavailable", "attempts": attempt}, status_code=503)
            if not budget.try_spend():
                stats.inc("errors")
                stats.inc("retries_budget_denied")
                rlog.log(logging.ERROR, "budget", "checkout failed: retry budget exhausted after %d attempt(s)", attempt)
                return JSONResponse({"error": "payments unavailable", "attempts": attempt}, status_code=503)
            stats.observe("backoff", delay * 1000)
            await asyncio.sleep(delay)
    finally:
        _in_flight -= 1
        stats.observe("request", (time.monotonic() - t0) * 1000)


@app.get("/stats")
async def get_stats():
    stats.gauges.update(max_retries=max_retries(), attempt_timeout_ms=attempt_timeout_s() * 1000,
                        in_flight=_in_flight, version=VERSION, effective_max_retries=effective_max_retries(),
                        retry_budget_tokens=round(budget.tokens, 3))
    return stats.snapshot()


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": VERSION}


class OverrideBody(BaseModel):
    max_retries: int | None = Field(None, ge=0, le=10)
    timeout_ms: int | None = Field(None, ge=50, le=10_000)  # clone lab (C6 retry_policy) only
    ttl_s: float = Field(gt=0)


def _override_state() -> dict[str, Any]:
    live = _override_live()
    remaining = max(0.0, _override["expires"] - time.monotonic()) if live else 0.0
    return {"max_retries": max_retries(), "attempt_timeout_ms": attempt_timeout_s() * 1000,
            "override": _override["max_retries"], "timeout_override_ms": (_override["timeout_s"] or 0) * 1000 or None,
            "remaining_s": round(remaining, 3)}


@app.post("/internal/retry_override", dependencies=[Depends(require_token)])
async def set_override(body: OverrideBody):
    if body.max_retries is None and body.timeout_ms is None:
        return JSONResponse({"detail": "max_retries or timeout_ms required"}, status_code=422)
    _override.update(max_retries=body.max_retries, expires=time.monotonic() + body.ttl_s,
                     timeout_s=body.timeout_ms / 1000.0 if body.timeout_ms is not None else None)
    log.info("retry override set: max_retries=%s%s for %.0fs", body.max_retries,
             f" timeout_ms={body.timeout_ms}" if body.timeout_ms is not None else "", body.ttl_s)
    return _override_state()


@app.delete("/internal/retry_override", dependencies=[Depends(require_token)])
async def clear_override():
    _override.update(max_retries=None, timeout_s=None, expires=0.0)
    return _override_state()


@app.get("/internal/retry_override", dependencies=[Depends(require_token)])
async def get_override():
    return _override_state()
