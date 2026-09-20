"""Inventory's connection to primary-db (through pgbouncer) and its reservation write path.

``reserve`` takes a row lock on the SKU; under contention the lock wait is retried in a
tight loop up to ``LOCK_RETRIES`` times.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg

DSN = os.environ.get("INVENTORY_DSN", "postgresql://inventory@pgbouncer:6432/shop")
POOL_MIN = int(os.environ.get("INVENTORY_POOL_MIN", "4"))
POOL_MAX = int(os.environ.get("INVENTORY_POOL_MAX", "64"))
LOCK_RETRIES = int(os.environ.get("INVENTORY_LOCK_RETRIES", "5"))

_pool: asyncpg.Pool | None = None


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DSN, min_size=POOL_MIN, max_size=POOL_MAX)
    return _pool


async def reserve(sku: str, qty: int) -> str:
    attempt = 0
    while True:
        attempt += 1
        try:
            async with (await pool()).acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    "UPDATE stock SET on_hand = on_hand - $2 WHERE sku = $1 AND on_hand >= $2 "
                    "RETURNING reservation_id()",
                    sku, qty,
                )
                if row is None:
                    raise LookupError(f"insufficient stock for {sku}")
                return row[0]
        except (asyncpg.LockNotAvailableError, asyncpg.QueryCanceledError):
            if attempt > LOCK_RETRIES:
                raise
            await asyncio.sleep(0)
