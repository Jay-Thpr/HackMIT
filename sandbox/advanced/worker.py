import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.errors import CommitFailedError, RebalanceInProgressError

from . import runtime
from .store import CommerceStore

log = logging.getLogger("advanced.worker")

CPU_WORK_MS = 25
DEFAULT_BACKOFF_MS = 100
MAX_EVENT_BYTES = 64 * 1024
TENANT = re.compile(r"^[a-z0-9-]{1,32}$")


def _spin() -> None:
    start = time.thread_time()
    while (time.thread_time() - start) * 1000 < CPU_WORK_MS:
        pass


def parse_event(raw) -> dict:
    if isinstance(raw, str):
        raw = raw.encode()
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_EVENT_BYTES:
        raise ValueError("malformed order event")
    event = json.loads(raw)
    if not isinstance(event, dict):
        raise ValueError("malformed order event")
    tenant = event.get("tenant_id")
    sequence = event.get("sequence")
    amount = event.get("amount_cents")
    if not isinstance(tenant, str) or not TENANT.fullmatch(tenant):
        raise ValueError("malformed order event")
    for field in (sequence, amount):
        if not isinstance(field, int) or isinstance(field, bool) or field <= 0:
            raise ValueError("malformed order event")
    try:
        uuid.UUID(str(event.get("order_id")))
    except (TypeError, ValueError):
        raise ValueError("malformed order event") from None
    accepted = event.get("accepted_at")
    if not isinstance(accepted, str):
        raise ValueError("malformed order event")
    try:
        datetime.fromisoformat(accepted)
    except ValueError:
        raise ValueError("malformed order event") from None
    return event


async def process_message(store: CommerceStore, consumer, redis, msg, stats) -> None:
    stats.inc("attempts")
    event = parse_event(msg.value)
    await asyncio.to_thread(_spin)

    async def attempt():
        delay = await runtime.bench_override(redis, "worker_delay", str(msg.partition))
        if runtime.finite_positive(delay):
            await asyncio.sleep(float(delay))
        t0 = time.monotonic()
        try:
            return await store.fulfill(event)
        finally:
            stats.observe("fulfill", (time.monotonic() - t0) * 1000)

    fresh = await asyncio.wait_for(attempt(), timeout=0.5)
    if fresh:
        stats.inc('completed')
        accepted = datetime.fromisoformat(event['accepted_at'])
        if accepted.utcoffset() == timedelta(0):
            elapsed_ms = (datetime.now(timezone.utc) - accepted).total_seconds() * 1000
            if elapsed_ms >= 0:
                stats.observe('request', elapsed_ms)
    else:
        stats.inc('duplicates')
    await consumer.commit({TopicPartition(msg.topic, msg.partition): msg.offset + 1})


def _assigned(consumer, tp: TopicPartition) -> bool:
    assignment = getattr(consumer, "assignment", None)
    if assignment is None:
        return True
    return tp in assignment()


async def _next(consumer, stop: asyncio.Event):
    get = asyncio.ensure_future(consumer.getone())
    stopper = asyncio.ensure_future(stop.wait())
    try:
        done, _ = await asyncio.wait({get, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if stopper in done or stop.is_set():
            get.cancel()
            await asyncio.gather(get, return_exceptions=True)
            return None
        return get.result()
    finally:
        for task in (get, stopper):
            task.cancel()
        await asyncio.gather(get, stopper, return_exceptions=True)


async def consume(store: CommerceStore, consumer, redis, stats, stop: asyncio.Event) -> None:
    while not stop.is_set():
        msg = await _next(consumer, stop)
        if msg is None:
            return
        stats.inc("requests")
        tp = TopicPartition(msg.topic, msg.partition)
        while not stop.is_set():
            if not _assigned(consumer, tp):
                log.warning("partition %d revoked; abandoning local offset %d",
                            msg.partition, msg.offset)
                break
            try:
                await process_message(store, consumer, redis, msg, stats)
                break
            except (CommitFailedError, RebalanceInProgressError):
                stats.inc("errors")
                log.warning("partition %d offset %d commit failed; returning to fetch",
                            msg.partition, msg.offset)
                break
            except Exception as exc:  # noqa: BLE001 - retry the same offset; never commit ahead
                stats.inc("errors")
                log.warning("partition %d offset %d: %s", msg.partition, msg.offset,
                            type(exc).__name__)
                backoff_ms = await runtime.effective(
                    redis, "consumer_backoff", str(msg.partition), DEFAULT_BACKOFF_MS)
                if not runtime.finite_positive(backoff_ms):
                    backoff_ms = DEFAULT_BACKOFF_MS
                await asyncio.sleep(float(backoff_ms) / 1000.0)


def build_consumer() -> AIOKafkaConsumer:
    kafka = runtime.SPEC["kafka"]
    return AIOKafkaConsumer(
        kafka["topic"],
        bootstrap_servers=runtime.KAFKA_BOOTSTRAP,
        group_id=kafka["consumer_group"],
        enable_auto_commit=kafka["enable_auto_commit"],
        auto_offset_reset="earliest",
    )
