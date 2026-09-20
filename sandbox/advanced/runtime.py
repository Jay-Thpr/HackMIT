import hashlib
import json
import math
import os
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parent / "spec.json"


def load_spec(path: Path = SPEC_PATH) -> dict:
    return json.loads(Path(path).read_text())


SPEC = load_spec()
NAMESPACE = os.environ.get("NAMESPACE", SPEC["namespace"])
ROLE = os.environ.get("ROLE", "api")
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
REPLICATION_PASSWORD = os.environ.get("REPLICATION_PASSWORD", "")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
CONTROL_TOKEN = os.environ.get("CONTROL_TOKEN", "")
LOAD_RPS = os.environ.get("LOAD_RPS")


def shard_for(tenant_id: str) -> int:
    return int.from_bytes(hashlib.sha256(tenant_id.encode()).digest()[:8], "big") % 3


def primary_dsn(shard: int) -> str:
    return f"postgresql://{SPEC['postgres']['user']}:{DB_PASSWORD}@shard-{shard}-primary:5432/{SPEC['postgres']['database']}"


def replica_dsn(shard: int) -> str:
    return f"postgresql://{SPEC['postgres']['user']}:{DB_PASSWORD}@shard-{shard}-replica:5432/{SPEC['postgres']['database']}"


def redis_url() -> str:
    return f"redis://:{REDIS_PASSWORD}@redis:6379/0"


def _decode(raw) -> object:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    return raw


async def _read(redis, key: str, default):
    raw = _decode(await redis.get(key))
    if raw is None:
        return default
    ttl = await redis.pttl(key)
    if not isinstance(ttl, int) or ttl <= 0:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    return default if value is None else value


def finite_positive(value) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


async def effective(redis, name: str, scope: str, default):
    return await _read(redis, f"control:{name}:{scope}", default)


async def bench_override(redis, name: str, scope: str, default=None):
    return await _read(redis, f"bench:{name}:{scope}", default)
