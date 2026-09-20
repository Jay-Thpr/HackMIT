import asyncio
import json
import logging
import signal

from aiokafka import AIOKafkaProducer

from . import runtime
from .sql import MARK_PUBLISHED, PENDING_OUTBOX
from .store import CommerceStore

log = logging.getLogger("advanced.relay")


def _encode(row) -> bytes:
    return json.dumps({
        "tenant_id": row["tenant_id"],
        "order_id": str(row["order_id"]),
        "sequence": row["sequence"],
        "amount_cents": row["amount_cents"],
        "accepted_at": row["accepted_at"].isoformat(),
    }).encode()


async def relay_shard(store: CommerceStore, producer, shard: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        pending = 0
        try:
            async with store.primaries[shard].acquire() as conn:
                async with conn.transaction():
                    rows = await conn.fetch(PENDING_OUTBOX)
                    pending = len(rows)
                    for row in rows:
                        await producer.send_and_wait(
                            runtime.SPEC["kafka"]["topic"],
                            _encode(row),
                            key=row["tenant_id"].encode(),
                        )
                        await conn.execute(MARK_PUBLISHED, row["tenant_id"], row["order_id"])
        except Exception as exc:  # noqa: BLE001 - crash duplicates are deduplicated downstream
            log.warning("relay shard %d publish failed: %s", shard, type(exc).__name__)
            await asyncio.sleep(0.5)
            continue
        if pending == 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                pass


async def run(store: CommerceStore, producer, stop: asyncio.Event) -> None:
    tasks = [
        asyncio.create_task(relay_shard(store, producer, shard, stop))
        for shard in range(runtime.SPEC["postgres"]["shards"])
    ]
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def build_producer() -> AIOKafkaProducer:
    kafka = runtime.SPEC["kafka"]
    return AIOKafkaProducer(
        bootstrap_servers=runtime.KAFKA_BOOTSTRAP,
        acks=kafka["producer_acks"],
        enable_idempotence=kafka["enable_idempotence"],
    )


async def _amain() -> None:
    store = CommerceStore()
    await store.connect()
    producer = build_producer()
    await producer.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await run(store, producer, stop)
    finally:
        await producer.stop()
        await store.close()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
