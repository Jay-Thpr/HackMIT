"""POST /checkout: reserve inventory, charge payment, write the order.

Each downstream hop runs through ``_call`` which applies ``DEFAULT_POLICY``. A slow
dependency therefore costs (1 + max_retries) attempts per checkout at this hop alone, and
the gateway in front of us retries the whole checkout again on 503/504.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from fastapi import FastAPI, HTTPException

from .retry_policy import DEFAULT_POLICY, RetryPolicy

log = logging.getLogger("shop.api")
app = FastAPI(title="shop-api")

INVENTORY_URL = "http://inventory:8001/reserve"
PAYMENTS_URL = "http://payments:8002/charge"


async def _call(client: httpx.AsyncClient, url: str, body: dict, policy: RetryPolicy = DEFAULT_POLICY) -> dict:
    attempt = 0
    while True:
        attempt += 1
        status, timed_out = None, False
        try:
            response = await client.post(url, json=body, timeout=policy.attempt_timeout_s)
            status = response.status_code
            if status < 500:
                response.raise_for_status()
                return response.json()
        except httpx.TimeoutException:
            timed_out = True
        except httpx.HTTPStatusError as exc:
            raise HTTPException(exc.response.status_code, exc.response.text) from exc
        if not policy.should_retry(attempt, status, timed_out):
            raise HTTPException(503, f"{url} unavailable after {attempt} attempts")
        await asyncio.sleep(policy.delay_s(attempt))


@app.post("/checkout")
async def checkout(order: dict) -> dict:
    t0 = time.monotonic()
    async with httpx.AsyncClient() as client:
        reservation = await _call(client, INVENTORY_URL, {"sku": order["sku"], "qty": order["qty"]})
        charge = await _call(client, PAYMENTS_URL, {"amount": order["amount"], "reservation": reservation["id"]})
    log.info("checkout ok in %.0f ms", (time.monotonic() - t0) * 1000)
    return {"order_id": charge["order_id"], "reservation": reservation["id"]}
