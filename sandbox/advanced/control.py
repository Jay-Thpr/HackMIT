import asyncio
import hmac
import json
import logging
import re
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from faultline_contracts.levers import (
    ActionHandle,
    ActionStatus,
    LeverKind,
    LeverSpec,
    LeverSpeed,
    UndoSpec,
)

from . import runtime

log = logging.getLogger("advanced.control")

TENANTS = tuple(runtime.SPEC["workload"]["tenants"])
WORKERS = tuple(f"worker-{i}" for i in range(runtime.SPEC["replicas"]["worker"]))
PARTITIONS = runtime.SPEC["kafka"]["partitions"]
SHARDS = runtime.SPEC["postgres"]["shards"]
ACTION_BUDGET = 5
INCIDENT_TTL_S = 3600
LEDGER_TTL_S = 3600
LEASE_PREFIX = "cpu_lease:"
INCIDENT_ID = re.compile(r"^[a-z0-9-]{1,128}$")


def _obj(props, required):
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


CATALOG = [
    LeverSpec(
        id="tenant_admission", kind=LeverKind.call_policy,
        description="Cap accepted orders per second for one tenant.",
        params_schema=_obj({"tenant": {"enum": list(TENANTS)},
                            "max_rps": {"type": "number", "minimum": 1, "maximum": 1000}},
                           ["tenant", "max_rps"]),
        speed=LeverSpeed.config, default_watch_s=20, max_ttl_s=120),
    LeverSpec(
        id="consumer_backoff", kind=LeverKind.call_policy,
        description="Slow fulfillment retries on one Kafka partition.",
        params_schema=_obj({"partition": {"type": "integer", "minimum": 0, "maximum": PARTITIONS - 1},
                            "backoff_ms": {"type": "integer", "minimum": 100, "maximum": 2000}},
                           ["partition", "backoff_ms"]),
        speed=LeverSpeed.config, default_watch_s=20, max_ttl_s=120),
    LeverSpec(
        id="read_route", kind=LeverKind.dependency,
        description="Route one shard's order reads to the primary or its replica.",
        params_schema=_obj({"shard": {"type": "integer", "minimum": 0, "maximum": SHARDS - 1},
                            "target": {"enum": ["primary", "replica"]}},
                           ["shard", "target"]),
        speed=LeverSpeed.config, default_watch_s=20, max_ttl_s=120),
    LeverSpec(
        id="cache_coalescing", kind=LeverKind.call_policy,
        description="Coalesce concurrent catalog misses for one tenant behind a lease.",
        params_schema=_obj({"tenant": {"enum": list(TENANTS)},
                            "enabled": {"type": "boolean"}},
                           ["tenant", "enabled"]),
        speed=LeverSpeed.config, default_watch_s=20, max_ttl_s=120),
    LeverSpec(
        id="worker_cpu", kind=LeverKind.dependency,
        description="Bound the CPU of one worker deployment via its resource limits.",
        params_schema=_obj({"worker": {"enum": list(WORKERS)},
                            "millicores": {"type": "integer", "minimum": 50, "maximum": 1000}},
                           ["worker", "millicores"]),
        speed=LeverSpeed.config, default_watch_s=20, max_ttl_s=120),
]
_LEVERS = {lever.id: lever for lever in CATALOG}


def _int(value, lo, hi):
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise HTTPException(422, f"expected integer in [{lo}, {hi}]")
    return value


def _number(value, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not (lo <= value <= hi):
        raise HTTPException(422, f"expected number in [{lo}, {hi}]")
    return float(value)


def _enum(value, choices):
    if value not in choices:
        raise HTTPException(422, f"expected one of {sorted(choices)}")
    return value


def _resolve(lever_id: str, params: dict) -> tuple[str, object]:
    if not isinstance(params, dict):
        raise HTTPException(422, "params must be an object")
    schema = _LEVERS[lever_id].params_schema
    required = set(schema["required"])
    if set(params) != required:
        raise HTTPException(422, f"params must be exactly {sorted(required)}")
    if lever_id == "tenant_admission":
        tenant = _enum(params["tenant"], TENANTS)
        return f"control:tenant_admission:{tenant}", _number(params["max_rps"], 1, 1000)
    if lever_id == "consumer_backoff":
        partition = _int(params["partition"], 0, PARTITIONS - 1)
        return f"control:consumer_backoff:{partition}", _int(params["backoff_ms"], 100, 2000)
    if lever_id == "read_route":
        shard = _int(params["shard"], 0, SHARDS - 1)
        return f"control:read_route:shard-{shard}", _enum(params["target"], ("primary", "replica"))
    if lever_id == "cache_coalescing":
        tenant = _enum(params["tenant"], TENANTS)
        enabled = params["enabled"]
        if not isinstance(enabled, bool):
            raise HTTPException(422, "enabled must be a boolean")
        return f"control:cache_coalescing:{tenant}", enabled
    if lever_id == "worker_cpu":
        worker = _enum(params["worker"], WORKERS)
        return worker, _int(params["millicores"], 50, 1000)
    raise HTTPException(404, f"unknown lever {lever_id!r}")


_BUDGET_LUA = """
local n = tonumber(redis.call('get', KEYS[1]) or '0')
if n >= tonumber(ARGV[1]) then return -1 end
n = redis.call('incr', KEYS[1])
redis.call('expire', KEYS[1], ARGV[2])
return n
"""

_CAS_DELETE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""


NS_ALLOWED = re.compile(r"^faultline-advanced(-clone-[a-z0-9]{8})?$")


class KubeClient:
    TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

    def __init__(self, namespace: str, *, token_path: Path | None = None,
                 base_url: str = "https://kubernetes.default.svc"):
        if not NS_ALLOWED.fullmatch(namespace):
            raise ValueError(f"namespace {namespace!r} is not an advanced namespace")
        self.namespace = namespace
        self.base_url = base_url
        self._token_path = token_path or self.TOKEN_PATH

    def _request(self, method: str, path: str, body=None, timeout: float = 10):
        token = self._token_path.read_text().strip()
        import ssl
        context = ssl.create_default_context(cafile=str(self.CA_PATH))
        request = urllib.request.Request(
            f"{self.base_url}{path}", method=method,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json-patch+json" if method == "PATCH" else "application/json"},
            data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return json.loads(response.read())

    def list_pods(self, label_selector: str = "") -> dict:
        query = f"?labelSelector={urllib.parse.quote(label_selector, safe='=,')}" \
            if label_selector else ""
        return self._request("GET", f"/api/v1/namespaces/{self.namespace}/pods{query}")

    def _check_worker(self, name: str) -> None:
        if name not in WORKERS and name != "loadgen":
            raise ValueError(f"deployment {name!r} is not patchable by control")

    def get_deployment(self, name: str) -> dict:
        self._check_worker(name)
        return self._request("GET", f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}")

    def patch_deployment(self, name: str, resource_version: str, patch: list[dict]) -> dict:
        self._check_worker(name)
        ops = [{"op": "test", "path": "/metadata/resourceVersion", "value": resource_version},
               *patch]
        return self._request(
            "PATCH", f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}", ops)


def _cpu_fields(deployment: dict) -> dict:
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    resources = container.get("resources", {})
    return {"requests": resources.get("requests", {}).get("cpu"),
            "limits": resources.get("limits", {}).get("cpu")}


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    lever_id: str
    params: dict = Field(default_factory=dict)
    ttl_s: int = Field(gt=0)
    incident_id: str


_state: dict = {}


async def _controller(redis, kube: KubeClient, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            ok = True
            keys = [k async for k in redis.scan_iter(f"{LEASE_PREFIX}*")]
            for key in keys:
                raw = await redis.get(key)
                if raw is None:
                    continue
                lease = json.loads(raw)
                if lease.get("status") in ("conflict", "failed"):
                    ok = False
                due = lease.get("undo") or time.time() >= lease["expires_at"]
                if not due:
                    continue
                if not await _restore_lease(redis, kube, key, lease):
                    ok = False
            await _restore_pauses(redis)
            _state["controller_ok"] = ok
        except Exception as exc:  # noqa: BLE001 - the loop must survive transient failures
            _state["controller_ok"] = False
            log.warning("lease controller: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass


CLONE_NS = re.compile(r"^faultline-advanced-clone-[a-z0-9]{8}$")
PAUSE_PREFIX = "pause_lease:"


async def _replica_exec(shard: int, statement: str) -> None:
    import asyncpg
    conn = await asyncpg.connect(runtime.replica_dsn(shard), timeout=5)
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


async def _replica_scalar(shard: int, statement: str):
    import asyncpg
    conn = await asyncpg.connect(runtime.replica_dsn(shard), timeout=5)
    try:
        return await conn.fetchval(statement)
    finally:
        await conn.close()


async def _restore_pauses(redis) -> None:
    from . import sql
    keys = [k async for k in redis.scan_iter(f"{PAUSE_PREFIX}*")]
    for key in keys:
        raw = await redis.get(key)
        if raw is None:
            continue
        lease = json.loads(raw)
        if time.time() < lease["expires_at"] and not lease.get("undo"):
            continue
        try:
            await _replica_exec(lease["shard"], sql.RESUME_REPLAY)
            paused = await _replica_scalar(lease["shard"], sql.REPLAY_PAUSED)
        except Exception:
            lease["status"] = "failed"
            await redis.set(key, json.dumps(lease))
            continue
        if paused is False:
            await redis.delete(key)
        else:
            lease["status"] = "failed"
            await redis.set(key, json.dumps(lease))


async def _restore_lease(redis, kube: KubeClient, key, lease: dict) -> bool:
    try:
        deployment = await asyncio.to_thread(kube.get_deployment, lease["worker"])
    except Exception:
        return False
    annotations = deployment["metadata"].get("annotations") or {}
    current = _cpu_fields(deployment)
    owner = annotations.get("faultline.dev/action-id")
    if owner != lease["action_id"]:
        if owner is None and current == lease["original"]:
            await redis.delete(key)
            return True
        lease["status"] = "conflict"
        await redis.set(key, json.dumps(lease))
        return False
    target = {"requests": f"{lease['millicores']}m", "limits": f"{lease['millicores']}m"}
    if current != target:
        lease["status"] = "conflict"
        await redis.set(key, json.dumps(lease))
        return False
    original = lease["original"]
    ops = []
    for scope in ("requests", "limits"):
        path = f"/spec/template/spec/containers/0/resources/{scope}/cpu"
        if original[scope] is None:
            if current[scope] is not None:
                ops.append({"op": "remove", "path": path})
        else:
            ops.append({"op": "add", "path": path, "value": original[scope]})
    ops.append({"op": "remove", "path": "/metadata/annotations/faultline.dev~1action-id"})
    try:
        await asyncio.to_thread(
            kube.patch_deployment, lease["worker"],
            deployment["metadata"]["resourceVersion"], ops)
    except Exception:
        return False
    try:
        verify = await asyncio.to_thread(kube.get_deployment, lease["worker"])
    except Exception:
        return False
    verified_annotations = verify["metadata"].get("annotations") or {}
    if _cpu_fields(verify) == original \
            and verified_annotations.get("faultline.dev/action-id") is None:
        await redis.delete(key)
        return True
    return False


async def _reconcile_leases(redis, kube: KubeClient) -> None:
    keys = [k async for k in redis.scan_iter(f"{LEASE_PREFIX}*")]
    for key in keys:
        raw = await redis.get(key)
        if raw is None:
            continue
        lease = json.loads(raw)
        if time.time() >= lease["expires_at"] or lease.get("undo"):
            await _restore_lease(redis, kube, key, lease)
    await _restore_pauses(redis)


def _http_get(url: str, timeout: float = 2) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        data = json.loads(response.read())
    if not isinstance(data, dict):
        raise ValueError("non-object stats response")
    return data


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from aiokafka import AIOKafkaConsumer
    from .store import CommerceStore
    redis = aioredis.from_url(runtime.redis_url())
    kube = KubeClient(runtime.NAMESPACE)
    store = CommerceStore()
    consumer = None
    try:
        await store.connect()
        consumer = AIOKafkaConsumer(
            bootstrap_servers=runtime.KAFKA_BOOTSTRAP,
            group_id="fulfillment", enable_auto_commit=False)
        await consumer.start()
    except Exception:
        if consumer is not None:
            await consumer.stop()
        await store.close()
        await redis.aclose()
        raise
    _state.update({"redis": redis, "kube": kube, "store": store,
                   "consumer": consumer, "http": _http_get, "controller_ok": True})
    stop = asyncio.Event()
    _state["stop"] = stop
    try:
        await _reconcile_leases(redis, kube)
    except Exception:
        _state["controller_ok"] = False
    task = asyncio.create_task(_controller(redis, kube, stop))
    _state["task"] = task
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
        await consumer.stop()
        await store.close()
        await redis.aclose()


app = FastAPI(lifespan=lifespan)


def _auth(request: Request) -> None:
    token = runtime.CONTROL_TOKEN
    header = request.headers.get("authorization", "")
    if not token or not header.startswith("Bearer ") \
            or not hmac.compare_digest(header[7:], token):
        raise HTTPException(401, "control token required")


@app.get("/healthz")
async def healthz():
    problems = []
    redis = _state.get("redis")
    if redis is None:
        problems.append("redis")
    else:
        try:
            await redis.ping()
        except Exception:
            problems.append("redis")
    store = _state.get("store")
    if store is None:
        problems.append("store")
    else:
        try:
            if not await store.healthy():
                problems.append("store")
        except Exception:
            problems.append("store")
    consumer = _state.get("consumer")
    if consumer is None:
        problems.append("kafka")
    else:
        try:
            if not consumer._client.cluster.brokers():
                problems.append("kafka")
        except Exception:
            problems.append("kafka")
    task = _state.get("task")
    if task is None or task.done():
        problems.append("controller")
    if not _state.get("controller_ok", True):
        problems.append("leases")
    if problems:
        raise HTTPException(503, f"unhealthy: {', '.join(problems)}")
    return {"ok": True}


@app.get("/catalog")
async def catalog(request: Request):
    _auth(request)
    return [lever.model_dump(mode="json") for lever in CATALOG]


@app.get("/snapshot")
async def snapshot(request: Request):
    _auth(request)
    from . import observe
    try:
        return await observe.snapshot(
            store=_state.get("store"), redis=_state.get("redis"), kube=_state.get("kube"),
            consumer=_state.get("consumer"), http=_state.get("http"))
    except Exception as exc:
        raise HTTPException(503, f"snapshot unavailable: {type(exc).__name__}") from exc


@app.post("/actions", status_code=201)
async def apply(request: Request, body: ActionBody):
    _auth(request)
    lever = _LEVERS.get(body.lever_id)
    if lever is None:
        raise HTTPException(404, f"unknown lever {body.lever_id!r}")
    if not INCIDENT_ID.fullmatch(body.incident_id):
        raise HTTPException(422, "bad incident_id")
    if body.ttl_s > lever.max_ttl_s:
        raise HTTPException(422, f"ttl_s exceeds max {lever.max_ttl_s}")
    redis = _state["redis"]
    scope, value = _resolve(body.lever_id, body.params)
    budget_key = f"action_budget:{body.incident_id}"
    taken = await redis.eval(_BUDGET_LUA, 1, budget_key, str(ACTION_BUDGET), str(INCIDENT_TTL_S))
    if int(taken) < 0:
        raise HTTPException(429, f"action budget {ACTION_BUDGET} exhausted for {body.incident_id}")
    action_id = f"act-{uuid.uuid4().hex[:12]}"
    try:
        if body.lever_id == "worker_cpu":
            await _apply_worker_cpu(redis, _state["kube"], action_id, scope, value, body.ttl_s)
        else:
            payload = json.dumps({"action_id": action_id, "value": value})
            if not await redis.set(scope, payload, nx=True, px=body.ttl_s * 1000):
                raise HTTPException(409, f"{scope} already controlled by another action")
    except Exception:
        await redis.decr(budget_key)
        raise
    handle = ActionHandle(
        action_id=action_id, lever_id=body.lever_id, params=body.params, ttl_s=body.ttl_s,
        undo=UndoSpec(lever_id=body.lever_id, payload={"scope": scope, "value": value}),
    )
    ledger = {"handle": handle.model_dump(mode="json"), "scope": scope, "value": value,
              "incident_id": body.incident_id}
    await redis.set(f"action:{action_id}", json.dumps(ledger), ex=LEDGER_TTL_S)
    return handle.model_dump(mode="json")


async def _lease_applied(kube: KubeClient, worker: str, action_id: str) -> bool | None:
    try:
        deployment = await asyncio.to_thread(kube.get_deployment, worker)
    except Exception:
        return None
    annotations = deployment["metadata"].get("annotations") or {}
    return annotations.get("faultline.dev/action-id") == action_id


async def _apply_worker_cpu(redis, kube: KubeClient, action_id: str, worker: str,
                            millicores: int, ttl_s: int) -> None:
    try:
        deployment = await asyncio.to_thread(kube.get_deployment, worker)
    except Exception as exc:
        raise HTTPException(502, f"could not read {worker}: {type(exc).__name__}") from exc
    key = f"{LEASE_PREFIX}{worker}"
    lease = {
        "action_id": action_id,
        "worker": worker,
        "millicores": millicores,
        "original": _cpu_fields(deployment),
        "expires_at": time.time() + ttl_s,
        "status": "active",
    }
    if not await redis.set(key, json.dumps(lease), nx=True):
        raise HTTPException(409, f"{worker} already has an active CPU lease")
    annotations = deployment["metadata"].get("annotations")
    if annotations is None:
        annotation_ops = [{"op": "add", "path": "/metadata/annotations",
                           "value": {"faultline.dev/action-id": action_id}}]
    else:
        annotation_ops = [{"op": "add", "path": "/metadata/annotations/faultline.dev~1action-id",
                           "value": action_id}]
    patch = [
        {"op": "add", "path": "/spec/template/spec/containers/0/resources/requests/cpu",
         "value": f"{millicores}m"},
        {"op": "add", "path": "/spec/template/spec/containers/0/resources/limits/cpu",
         "value": f"{millicores}m"},
        *annotation_ops,
    ]
    try:
        await asyncio.to_thread(
            kube.patch_deployment, worker, deployment["metadata"]["resourceVersion"], patch)
    except Exception as exc:
        applied = await _lease_applied(kube, worker, action_id)
        if applied is False:
            await redis.delete(key)
        raise HTTPException(502, f"could not patch {worker}: {type(exc).__name__}") from exc


async def _load_ledger(redis, action_id: str) -> dict:
    raw = await redis.get(f"action:{action_id}")
    if raw is None:
        raise HTTPException(404, f"unknown action {action_id!r}")
    return json.loads(raw)


def _observed_status(redis_value: bytes | str | None, action_id: str,
                     declared: ActionStatus) -> ActionStatus:
    if declared != ActionStatus.active:
        return declared
    if redis_value is None:
        return ActionStatus.expired
    try:
        current = json.loads(redis_value)
    except (TypeError, json.JSONDecodeError):
        return ActionStatus.failed
    if current.get("action_id") != action_id:
        return ActionStatus.failed
    return ActionStatus.active


@app.get("/actions/{action_id}")
async def status(request: Request, action_id: str):
    _auth(request)
    redis = _state["redis"]
    ledger = await _load_ledger(redis, action_id)
    handle = ActionHandle(**ledger["handle"])
    if handle.status == ActionStatus.active:
        if handle.lever_id == "worker_cpu":
            raw = await redis.get(f"{LEASE_PREFIX}{ledger['scope']}")
            if raw is None:
                observed = ActionStatus.expired
            else:
                lease = json.loads(raw)
                if lease.get("action_id") != action_id:
                    observed = ActionStatus.expired
                else:
                    observed = ActionStatus.failed \
                        if lease.get("status") in ("conflict", "failed") \
                        else ActionStatus.active
        else:
            observed = _observed_status(await redis.get(ledger["scope"]), action_id,
                                        ActionStatus.active)
        handle = handle.model_copy(update={"status": observed})
    return handle.model_dump(mode="json")


@app.delete("/actions/{action_id}")
async def undo(request: Request, action_id: str):
    _auth(request)
    redis = _state["redis"]
    ledger = await _load_ledger(redis, action_id)
    handle = ActionHandle(**ledger["handle"])
    if handle.status != ActionStatus.active:
        return handle.model_dump(mode="json")
    if handle.lever_id == "worker_cpu":
        key = f"{LEASE_PREFIX}{ledger['scope']}"
        raw = await redis.get(key)
        if raw is None:
            observed = ActionStatus.expired
        else:
            lease = json.loads(raw)
            if lease.get("action_id") != action_id:
                observed = ActionStatus.failed
            else:
                lease["undo"] = True
                await redis.set(key, json.dumps(lease))
                ok = await _restore_lease(redis, _state["kube"], key, lease)
                observed = ActionStatus.undone if ok else ActionStatus.failed
    else:
        expected = json.dumps({"action_id": action_id, "value": ledger["value"]})
        if await redis.get(ledger["scope"]) is None:
            observed = ActionStatus.expired
        else:
            removed = await redis.eval(_CAS_DELETE_LUA, 1, ledger["scope"], expected)
            observed = ActionStatus.undone if int(removed) else ActionStatus.failed
    handle = handle.model_copy(update={"status": observed})
    ledger["handle"] = handle.model_dump(mode="json")
    await redis.set(f"action:{action_id}", json.dumps(ledger), ex=LEDGER_TTL_S)
    return handle.model_dump(mode="json")


def estimate_blast_radius(lever_id: str, params: dict) -> float:
    if lever_id not in _LEVERS:
        raise HTTPException(404, f"unknown lever {lever_id!r}")
    return 100.0


class LabActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: str
    params: dict = Field(default_factory=dict)
    ttl_s: int = Field(gt=0)


_LAB_MAX_TTL_S = 120


def _clone_only() -> None:
    if not CLONE_NS.fullmatch(runtime.NAMESPACE):
        raise HTTPException(404, "lab actions exist only on clone namespaces")


def _lab_int(value, lo, hi):
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise HTTPException(422, f"expected integer in [{lo}, {hi}]")
    return value


def _lab_number(value, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not (lo < value <= hi):
        raise HTTPException(422, f"expected number in ({lo}, {hi}]")
    return float(value)


@app.post("/lab/actions", status_code=201)
async def lab_apply(request: Request, body: LabActionBody):
    _clone_only()
    _auth(request)
    redis = _state["redis"]
    action_id = f"lab-{uuid.uuid4().hex[:12]}"
    params = body.params
    if body.ttl_s > _LAB_MAX_TTL_S:
        raise HTTPException(422, f"ttl_s exceeds {_LAB_MAX_TTL_S}")
    if body.action == "worker_delay":
        if set(params) != {"partition", "delay_ms"}:
            raise HTTPException(422, "params must be partition and delay_ms")
        partition = _lab_int(params["partition"], 0, PARTITIONS - 1)
        delay_ms = _lab_int(params["delay_ms"], 0, 5000)
        payload = json.dumps({"action_id": action_id, "value": delay_ms / 1000.0})
        if not await redis.set(f"bench:worker_delay:{partition}", payload,
                               nx=True, px=body.ttl_s * 1000):
            raise HTTPException(409, "worker_delay already active on that partition")
    elif body.action == "replica_pause":
        if set(params) != {"shard"}:
            raise HTTPException(422, "params must be shard")
        shard = _lab_int(params["shard"], 0, SHARDS - 1)
        lease = {"action_id": action_id, "shard": shard,
                 "expires_at": time.time() + body.ttl_s, "status": "active"}
        key = f"{PAUSE_PREFIX}{shard}"
        if not await redis.set(key, json.dumps(lease), nx=True):
            raise HTTPException(409, f"replica {shard} already paused by another action")
        from . import sql
        try:
            already = await _replica_scalar(shard, sql.REPLAY_PAUSED)
        except Exception:
            already = None
        if already is True:
            await redis.delete(key)
            raise HTTPException(409, f"replica {shard} replay is already paused")
        if already is not False:
            await redis.delete(key)
            raise HTTPException(503, f"replica {shard} replay state unknown; refusing to pause")
        try:
            await _replica_exec(shard, sql.PAUSE_REPLAY)
        except Exception as exc:
            try:
                paused = await _replica_scalar(shard, sql.REPLAY_PAUSED)
            except Exception:
                paused = None
            if paused is False:
                await redis.delete(key)
            else:
                lease["undo"] = True
                lease["status"] = "failed"
                await redis.set(key, json.dumps(lease))
            raise HTTPException(502, f"replica {shard} pause unconfirmed") from exc
    elif body.action == "workload":
        if set(params) != {"extra_rps", "tenant"}:
            raise HTTPException(422, "params must be extra_rps and tenant")
        override = {"extra_rps": _number(params["extra_rps"], 0, 200),
                    "tenant": _enum(params["tenant"], TENANTS)}
        payload = json.dumps({"action_id": action_id, "value": override})
        if not await redis.set("bench:workload:global", payload, nx=True,
                               px=body.ttl_s * 1000):
            raise HTTPException(409, "workload override already active")
    elif body.action == "cache_policy":
        if set(params) != {"tenant", "ttl_s"}:
            raise HTTPException(422, "params must be tenant and ttl_s")
        tenant = _enum(params["tenant"], TENANTS)
        ttl = _lab_int(params["ttl_s"], 1, 30)
        payload = json.dumps({"action_id": action_id, "value": ttl})
        if not await redis.set(f"bench:cache_ttl:{tenant}", payload, nx=True,
                               px=body.ttl_s * 1000):
            raise HTTPException(409, "cache_policy already active for that tenant")
    elif body.action == "cpu_limit":
        if set(params) != {"worker", "cpus"}:
            raise HTTPException(422, "params must be worker and cpus")
        worker = _enum(params["worker"], WORKERS)
        cpus = _lab_number(params["cpus"], 0, 1)
        if cpus < 0.05:
            raise HTTPException(422, "cpus must be at least 0.05")
        await _apply_worker_cpu(redis, _state["kube"], action_id, worker,
                                max(50, round(cpus * 1000)), body.ttl_s)
    else:
        raise HTTPException(404, f"unknown lab action {body.action!r}")
    ledger = {"action_id": action_id, "action": body.action, "params": params,
              "ttl_s": body.ttl_s, "status": "active",
              "payload": locals().get("payload")}
    await redis.set(f"lab_action:{action_id}", json.dumps(ledger), ex=LEDGER_TTL_S)
    return {"action_id": action_id, "status": "active"}


@app.get("/lab/actions/{action_id}")
async def lab_status(request: Request, action_id: str):
    _clone_only()
    _auth(request)
    redis = _state["redis"]
    raw = await redis.get(f"lab_action:{action_id}")
    if raw is None:
        raise HTTPException(404, f"unknown lab action {action_id!r}")
    ledger = json.loads(raw)
    if ledger["status"] == "active":
        params = ledger["params"]
        if ledger["action"] == "replica_pause":
            lease_raw = await redis.get(f"{PAUSE_PREFIX}{params['shard']}")
            if lease_raw is None:
                ledger["status"] = "expired"
            else:
                lease = json.loads(lease_raw)
                if lease.get("action_id") != action_id:
                    ledger["status"] = "expired"
                elif lease.get("status") in ("conflict", "failed"):
                    ledger["status"] = "failed"
        elif ledger["action"] == "cpu_limit":
            lease_raw = await redis.get(f"{LEASE_PREFIX}{params['worker']}")
            if lease_raw is None:
                ledger["status"] = "expired"
            else:
                lease = json.loads(lease_raw)
                if lease.get("action_id") != action_id:
                    ledger["status"] = "expired"
                elif lease.get("status") in ("conflict", "failed"):
                    ledger["status"] = "failed"
        else:
            scope = {"worker_delay": f"bench:worker_delay:{params.get('partition')}",
                     "workload": "bench:workload:global",
                     "cache_policy": f"bench:cache_ttl:{params.get('tenant')}"}[ledger["action"]]
            if await redis.get(scope) is None:
                ledger["status"] = "expired"
    return {"action_id": action_id, "status": ledger["status"]}


@app.delete("/lab/actions/{action_id}")
async def lab_undo(request: Request, action_id: str):
    _clone_only()
    _auth(request)
    redis = _state["redis"]
    raw = await redis.get(f"lab_action:{action_id}")
    if raw is None:
        raise HTTPException(404, f"unknown lab action {action_id!r}")
    ledger = json.loads(raw)
    if ledger["status"] != "active":
        return {"action_id": action_id, "status": ledger["status"]}
    action, params = ledger["action"], ledger["params"]
    scope = {"worker_delay": f"bench:worker_delay:{params.get('partition')}",
             "workload": "bench:workload:global",
             "cache_policy": f"bench:cache_ttl:{params.get('tenant')}"}.get(action)
    if scope is not None:
        if await redis.get(scope) is None:
            status = "expired"
        else:
            removed = await redis.eval(_CAS_DELETE_LUA, 1, scope, ledger["payload"])
            status = "undone" if int(removed) else "failed"
    elif action == "replica_pause":
        key = f"{PAUSE_PREFIX}{params['shard']}"
        lease_raw = await redis.get(key)
        if lease_raw is None:
            status = "expired"
        else:
            lease = json.loads(lease_raw)
            if lease.get("action_id") != action_id:
                status = "failed"
            else:
                lease["undo"] = True
                await redis.set(key, json.dumps(lease))
                await _restore_pauses(redis)
                status = "undone" if await redis.get(key) is None else "failed"
    elif action == "cpu_limit":
        key = f"{LEASE_PREFIX}{params['worker']}"
        lease_raw = await redis.get(key)
        if lease_raw is None:
            status = "expired"
        else:
            lease = json.loads(lease_raw)
            if lease.get("action_id") != action_id:
                status = "failed"
            else:
                lease["undo"] = True
                await redis.set(key, json.dumps(lease))
                ok = await _restore_lease(redis, _state["kube"], key, lease)
                status = "undone" if ok else "failed"
    else:
        status = "failed"
    ledger["status"] = status
    await redis.set(f"lab_action:{action_id}", json.dumps(ledger), ex=LEDGER_TTL_S)
    return {"action_id": action_id, "status": status}
