"""Payments -> card processor client.

``charge`` is idempotent on ``idempotency_key`` so retrying is safe; how *much* it retries
is the question. Pre-incident: three immediate retries per charge, no budget, and a hedged
duplicate request fired after ``HEDGE_AFTER_MS`` on every call.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import httpx

PROCESSOR_URL = os.environ.get("PROCESSOR_URL", "https://processor.internal/v1/charge")
MAX_RETRIES = int(os.environ.get("PAYMENTS_MAX_RETRIES", "3"))
ATTEMPT_TIMEOUT_S = float(os.environ.get("PAYMENTS_ATTEMPT_TIMEOUT_MS", "800")) / 1000.0
HEDGE_AFTER_MS = int(os.environ.get("PAYMENTS_HEDGE_AFTER_MS", "200"))


async def _attempt(client: httpx.AsyncClient, body: dict) -> dict:
    response = await client.post(PROCESSOR_URL, json=body, timeout=ATTEMPT_TIMEOUT_S)
    response.raise_for_status()
    return response.json()


async def charge(client: httpx.AsyncClient, amount: int, reservation: str) -> dict:
    body = {"amount": amount, "reservation": reservation, "idempotency_key": uuid.uuid4().hex}
    attempt = 0
    while True:
        attempt += 1
        primary = asyncio.ensure_future(_attempt(client, body))
        hedge = asyncio.ensure_future(_hedged(client, body))
        done, pending = await asyncio.wait({primary, hedge}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        try:
            return done.pop().result()
        except (httpx.TimeoutException, httpx.HTTPStatusError):
            if attempt > MAX_RETRIES:
                raise


async def _hedged(client: httpx.AsyncClient, body: dict) -> dict:
    await asyncio.sleep(HEDGE_AFTER_MS / 1000.0)
    return await _attempt(client, body)
