import asyncio
import uuid

import asyncpg

from . import runtime
from .sql import (
    ADVANCE_PROGRESS,
    CATALOG_ITEM,
    ENSURE_PROGRESS,
    ENSURE_TENANT,
    EXISTING_ORDER,
    EXISTING_PAYMENT,
    FULFILL_ORDER,
    INSERT_ORDER,
    INSERT_OUTBOX,
    INSERT_PAYMENT,
    LOCK_PROGRESS,
    LOCK_TENANT,
    NEXT_SEQUENCE,
)

CONN_ERRORS = (asyncpg.PostgresError, OSError, asyncio.TimeoutError)
ACQUIRE_TIMEOUT_S = 5
COMMAND_TIMEOUT_S = 5
READ_TIMEOUT_S = 2


class ConflictError(Exception):
    pass


class SequenceGap(Exception):
    pass


class StoreUnavailable(Exception):
    pass


class CommerceStore:
    def __init__(self, primary_dsns: list[str] | None = None, replica_dsns: list[str] | None = None,
                 *, min_size: int = 1, max_size: int = 4):
        shards = runtime.SPEC["postgres"]["shards"]
        self._primary_dsns = primary_dsns or [runtime.primary_dsn(i) for i in range(shards)]
        self._replica_dsns = replica_dsns or [runtime.replica_dsn(i) for i in range(shards)]
        self._min_size, self._max_size = min_size, max_size
        self.primaries: list = []
        self.replicas: list = []

    async def connect(self) -> None:
        opened: list = []
        try:
            for dsn in self._primary_dsns:
                pool = await asyncpg.create_pool(
                    dsn, min_size=self._min_size, max_size=self._max_size,
                    timeout=ACQUIRE_TIMEOUT_S, command_timeout=COMMAND_TIMEOUT_S)
                opened.append(pool)
                self.primaries.append(pool)
            for dsn in self._replica_dsns:
                pool = await asyncpg.create_pool(
                    dsn, min_size=self._min_size, max_size=self._max_size,
                    timeout=ACQUIRE_TIMEOUT_S, command_timeout=COMMAND_TIMEOUT_S)
                opened.append(pool)
                self.replicas.append(pool)
        except Exception:
            for pool in opened:
                await pool.close()
            self.primaries = []
            self.replicas = []
            raise

    async def close(self) -> None:
        for pool in (*self.primaries, *self.replicas):
            await pool.close()

    async def healthy(self) -> bool:
        pools = (*self.primaries, *self.replicas)
        if len(pools) != len(self._primary_dsns) + len(self._replica_dsns):
            return False
        for pool in pools:
            if pool.is_closing():
                return False
            try:
                async with pool.acquire(timeout=READ_TIMEOUT_S) as conn:
                    await conn.fetchval("SELECT 1", timeout=READ_TIMEOUT_S)
            except Exception:
                return False
        return True

    async def accept(self, tenant_id: str, order_id: uuid.UUID, amount_cents: int) -> dict:
        async with self.primaries[runtime.shard_for(tenant_id)].acquire() as conn:
            async with conn.transaction():
                await conn.execute(LOCK_TENANT, tenant_id)
                await conn.execute(ENSURE_TENANT, tenant_id)
                existing = await conn.fetchrow(EXISTING_ORDER, tenant_id, order_id)
                if existing:
                    if existing["amount_cents"] != amount_cents:
                        raise ConflictError(
                            f"order {order_id} already accepted with amount {existing['amount_cents']}"
                        )
                    return dict(existing)
                seq = await conn.fetchval(NEXT_SEQUENCE, tenant_id)
                accepted_at = await conn.fetchval(INSERT_ORDER, tenant_id, order_id, seq, amount_cents)
                await conn.execute(INSERT_OUTBOX, tenant_id, order_id, seq)
                return {
                    "order_id": str(order_id),
                    "sequence": seq,
                    "amount_cents": amount_cents,
                    "accepted_at": accepted_at.isoformat(),
                }

    async def fulfill(self, event: dict) -> bool:
        tenant_id = event["tenant_id"]
        order_id = event["order_id"] if isinstance(event["order_id"], uuid.UUID) \
            else uuid.UUID(str(event["order_id"]))
        async with self.primaries[runtime.shard_for(tenant_id)].acquire() as conn:
            async with conn.transaction():
                await conn.execute(LOCK_TENANT, tenant_id)
                order = await conn.fetchrow(EXISTING_ORDER, tenant_id, order_id)
                if order is None or order["sequence"] != event["sequence"] \
                        or order["amount_cents"] != event["amount_cents"]:
                    raise ConflictError(f"fulfillment event does not match accepted order {order_id}")
                payment = await conn.fetchrow(EXISTING_PAYMENT, tenant_id, order_id)
                if payment is not None:
                    if payment["sequence"] != event["sequence"] \
                            or payment["amount_cents"] != event["amount_cents"]:
                        raise ConflictError(
                            f"existing payment for {order_id} does not match the event")
                    return False
                await conn.execute(ENSURE_PROGRESS, tenant_id)
                last = await conn.fetchval(LOCK_PROGRESS, tenant_id)
                if event["sequence"] != (last or 0) + 1:
                    raise SequenceGap(
                        f"tenant {tenant_id}: event sequence {event['sequence']} "
                        f"does not follow {last}"
                    )
                await conn.execute(INSERT_PAYMENT, tenant_id, order_id,
                                   event["sequence"], event["amount_cents"])
                await conn.execute(ADVANCE_PROGRESS, tenant_id, event["sequence"])
                await conn.execute(FULFILL_ORDER, tenant_id, order_id)
                return True

    async def _read_row(self, query, shard: int, args: tuple, *, strong: bool,
                        min_sequence: int | None = None):
        pools = [self.primaries[shard]] if strong else [self.replicas[shard], self.primaries[shard]]
        row = None
        failed = False
        for pool in pools:
            try:
                async with pool.acquire(timeout=ACQUIRE_TIMEOUT_S) as conn:
                    row = await conn.fetchrow(query, *args, timeout=READ_TIMEOUT_S)
            except CONN_ERRORS:
                row = None
                failed = True
                continue
            failed = False
            if row is not None and (min_sequence is None or row["sequence"] >= min_sequence):
                return row
            row = None
        if row is None and failed:
            raise StoreUnavailable("order store is unavailable")
        return row

    async def get_order(self, tenant_id: str, order_id: uuid.UUID, *, strong: bool = False,
                        min_sequence: int | None = None) -> dict | None:
        row = await self._read_row(
            EXISTING_ORDER, runtime.shard_for(tenant_id), (tenant_id, order_id),
            strong=strong, min_sequence=min_sequence)
        return dict(row) if row is not None else None

    async def catalog(self, tenant_id: str, item_id: int) -> dict | None:
        row = await self._read_row(
            CATALOG_ITEM, runtime.shard_for(tenant_id), (item_id,), strong=False)
        return dict(row) if row is not None else None
