import asyncio
import json
import math
import os
import re
import secrets
import subprocess
import tempfile
import time
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from faultline_contracts.clone import (
    CloneEndpoints,
    CloneInfo,
    CloneSpec,
    CloneStatus,
    LabActionHandle,
    LabActionRequest,
    LabActionSpec,
    LabError,
)
from faultline_contracts.levers import ActionStatus, _obj

from . import cli, manifests, runtime

HOST = "127.0.0.1"
PORT = 19920
STATE_FILE = cli.RUN_DIR / "lab-state.json"
CREDENTIALS_DIR = cli.RUN_DIR / "credentials"
CONTROL_PORT_BASE = 20000
CLONE_TTL_S = 3600
MAX_CLONES = 1
DESTROY_POLL_S = 30
MIN_DOCKER_BYTES = 12 * 1024**3
NS_PATTERN = manifests.NS_PATTERN
CLONE_NS = re.compile(r"^faultline-advanced-clone-[a-z0-9]{8}$")
CLONE_ID = re.compile(r"^adv-[0-9a-f]{10}$")

TENANTS = tuple(runtime.SPEC["workload"]["tenants"])
WORKERS = tuple(f"worker-{i}" for i in range(runtime.SPEC["replicas"]["worker"]))
PARTITIONS = runtime.SPEC["kafka"]["partitions"]
SHARDS = runtime.SPEC["postgres"]["shards"]

LAB_CATALOG = [
    LabActionSpec(
        id="worker_delay",
        description="Delay fulfillment attempts on one Kafka partition.",
        params_schema=_obj({"partition": {"type": "integer", "minimum": 0, "maximum": PARTITIONS - 1},
                            "delay_ms": {"type": "integer", "minimum": 0, "maximum": 5000}},
                           ["partition", "delay_ms"]),
        max_ttl_s=120),
    LabActionSpec(
        id="replica_pause",
        description="Pause WAL replay on one shard's streaming replica.",
        params_schema=_obj({"shard": {"type": "integer", "minimum": 0, "maximum": SHARDS - 1}},
                           ["shard"]),
        max_ttl_s=120),
    LabActionSpec(
        id="workload",
        description="Add extra open-loop request rate targeting one tenant.",
        params_schema=_obj({"extra_rps": {"type": "number", "minimum": 0, "maximum": 200},
                            "tenant": {"enum": list(TENANTS)}},
                           ["extra_rps", "tenant"]),
        max_ttl_s=120),
    LabActionSpec(
        id="cache_policy",
        description="Set the catalog cache TTL for one tenant.",
        params_schema=_obj({"tenant": {"enum": list(TENANTS)},
                            "ttl_s": {"type": "integer", "minimum": 1, "maximum": 30}},
                           ["tenant", "ttl_s"]),
        max_ttl_s=120),
    LabActionSpec(
        id="cpu_limit",
        description="Bound one worker deployment to `cpus` CPUs.",
        params_schema=_obj({"worker": {"enum": list(WORKERS)},
                            "cpus": {"type": "number", "minimum": 0.05, "maximum": 1}},
                           ["worker", "cpus"]),
        max_ttl_s=120),
]
LAB_ACTION_IDS = {a.id for a in LAB_CATALOG}

_state: dict = {"clones": {}, "forwards": {}, "expiry_task": None, "load_error": False}
_create_lock = asyncio.Lock()


def _save() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_FILE.parent, prefix="lab-state.")
    with os.fdopen(fd, "w") as fh:
        json.dump(_state["clones"], fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


_RECORD_STATUSES = {s.value for s in CloneStatus} | {"cleanup_pending", "quarantined"}


def _record_ok(clone_id: str, record) -> bool:
    if not (
        CLONE_ID.fullmatch(clone_id)
        and isinstance(record, dict)
        and CLONE_NS.fullmatch(str(record.get("namespace", "")))
        and record.get("status") in _RECORD_STATUSES
        and isinstance(record.get("expires_at"), (int, float))
        and not isinstance(record.get("expires_at"), bool)
        and math.isfinite(record.get("expires_at", 0))
        and isinstance(record.get("actions", []), list)
        and (record.get("namespace_uid") is None
             or isinstance(record.get("namespace_uid"), str))
    ):
        return False
    try:
        datetime.fromisoformat(str(record.get("created_at", "")))
        CloneSpec.model_validate(record.get("spec"))
    except Exception:
        return False
    return True


def _owned_namespace(record: dict) -> str:
    namespace = str(record.get("namespace", ""))
    if not CLONE_NS.fullmatch(namespace):
        raise LabError("recorded namespace is not a clone namespace; refusing to mutate")
    return namespace


def _load() -> None:
    _state["load_error"] = False
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict) or not all(
                _record_ok(cid, rec) for cid, rec in data.items()):
            _state["clones"] = {}
            _state["load_error"] = True
            return
        _state["clones"] = data
    for record in _state["clones"].values():
        if record.get("status") == CloneStatus.creating.value:
            record["status"] = CloneStatus.failed.value
            record["detail"] = "creation interrupted by manager restart"


def _kubectl(args: list[str], **kw) -> str:
    return cli._kubectl(args, **kw)


def _clone_namespace(clone_id: str) -> str:
    record = _state["clones"].get(clone_id)
    if record is None:
        raise LabError(f"unknown clone {clone_id!r}")
    return record["namespace"]


def _http_json(url: str, timeout: float = 3, method: str = "GET", token: str | None = None,
               body=None) -> dict:
    request = urllib.request.Request(url, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _to_info(clone_id: str, record: dict) -> dict:
    endpoints = None
    if record.get("port") and record["status"] == CloneStatus.ready.value:
        base = f"http://{HOST}:{record['port']}"
        endpoints = CloneEndpoints(
            gateway_url=base, control_url=base,
            stats_urls={"distributed": base}).model_dump(mode="json")
    spec = CloneSpec.model_validate(record["spec"])
    status = record["status"]
    if status not in {s.value for s in CloneStatus}:
        status = CloneStatus.failed.value
    info = CloneInfo(
        clone_id=clone_id,
        status=CloneStatus(status),
        spec=spec,
        created_at=datetime.fromisoformat(record["created_at"]),
        endpoints=CloneEndpoints.model_validate(endpoints) if endpoints else None,
        active_actions=[LabActionHandle.model_validate(a)
                        for a in record.get("actions", [])],
        detail=record.get("detail"),
    )
    return info.model_dump(mode="json")


def _docker_memory_bytes() -> int | None:
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{.MemTotal}}"],
            capture_output=True, text=True, timeout=15)
        return int(out.stdout.strip())
    except Exception:
        return None


def _preflight() -> None:
    if _state.get("load_error"):
        raise LabError("lab state file is corrupt; refusing to manage clones")
    if not cli.KUBECONFIG.exists() or not cli._cluster_exists():
        raise LabError("main faultline-advanced cluster with private kubeconfig is required")
    mem = _docker_memory_bytes()
    if mem is None or mem < MIN_DOCKER_BYTES:
        raise LabError("insufficient local memory headroom for an advanced clone (need >= 12GiB)")


def _token_path(clone_id: str) -> Path:
    return CREDENTIALS_DIR / f"{clone_id}.token"


def _write_token(clone_id: str, token: str) -> None:
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    path = _token_path(clone_id)
    fd, tmp = tempfile.mkstemp(dir=CREDENTIALS_DIR, prefix="token.")
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _drop_token(clone_id: str) -> None:
    try:
        _token_path(clone_id).unlink()
    except OSError:
        pass


def _start_forward(namespace: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        ["kubectl", "--kubeconfig", str(cli.KUBECONFIG), "--context", cli.CONTEXT,
         "-n", namespace, "port-forward", "--address", HOST,
         "svc/control", f"{port}:8000"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_ready(namespace: str, timeout_s: int = 300) -> None:
    for obj in manifests.render(namespace):
        if obj["kind"] in ("Deployment", "StatefulSet"):
            _kubectl(["rollout", "status", f"{obj['kind'].lower()}/{obj['metadata']['name']}",
                      "-n", namespace, f"--timeout={timeout_s}s"], timeout=timeout_s + 20)


def _snapshot_ok(snap: dict) -> bool:
    if snap.get("errors"):
        return False
    services = {inst.get("service") for inst in (snap.get("instances") or {}).values()}
    if not {"gateway", "fulfillment"} <= services:
        return False
    shards = (snap.get("business_snapshot") or {}).get("shards") or []
    if len(shards) != SHARDS or not all(s.get("complete") for s in shards):
        return False
    partitions = [k for k in (snap.get("resources") or {}) if k.startswith("kafka_partition_")]
    return len(partitions) == PARTITIONS


def _snapshot_activity(snap: dict) -> float:
    total = 0.0
    for inst in (snap.get("instances") or {}).values():
        total += float((inst.get("stats", {}).get("counters") or {}).get("requests", 0) or 0)
    for name, res in (snap.get("resources") or {}).items():
        if name.startswith("tenant_"):
            total += float(res.get("accepted_total", 0) or 0)
    return total


async def _expiry_loop() -> None:
    while True:
        for clone_id, record in list(_state["clones"].items()):
            status = record["status"]
            if status in (CloneStatus.failed.value, "cleanup_pending") or (
                    status != CloneStatus.destroyed.value
                    and time.time() >= record["expires_at"]):
                try:
                    await _destroy(clone_id)
                except Exception:
                    pass
        await asyncio.sleep(30)


def _namespace_uid(namespace: str) -> str | None:
    out = _kubectl(["get", "namespace", namespace, "--ignore-not-found", "-o", "json"])
    if not out.strip():
        return None
    return json.loads(out)["metadata"]["uid"]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _load()
    for clone_id, record in list(_state["clones"].items()):
        if record["status"] != CloneStatus.ready.value or not record.get("port"):
            continue
        try:
            if _namespace_uid(record["namespace"]) == record["namespace_uid"]:
                _state["forwards"][clone_id] = _start_forward(
                    record["namespace"], record["port"])
        except Exception:
            pass
    _state["expiry_task"] = asyncio.create_task(_expiry_loop())
    try:
        yield
    finally:
        _state["expiry_task"].cancel()
        await asyncio.gather(_state["expiry_task"], return_exceptions=True)
        for proc in _state["forwards"].values():
            proc.terminate()


app = FastAPI(lifespan=lifespan)


def _check_id(clone_id: str) -> None:
    if not CLONE_ID.fullmatch(clone_id):
        raise HTTPException(404, f"unknown clone {clone_id!r}")


@app.get("/healthz")
async def healthz():
    return {"ok": not _state.get("load_error", False), "clones_alive": sum(
        1 for r in _state["clones"].values() if r["status"] != CloneStatus.destroyed.value),
        "max_clones": MAX_CLONES}


@app.get("/lab/catalog")
async def catalog():
    return [a.model_dump(mode="json") for a in LAB_CATALOG]


@app.get("/clones")
async def list_clones():
    return [_to_info(cid, r) for cid, r in _state["clones"].items()]


async def _fail_creation(clone_id: str, exc: Exception) -> None:
    record = _state["clones"].get(clone_id)
    if record is not None:
        record["status"] = CloneStatus.failed.value
        record["detail"] = f"creation failed: {type(exc).__name__}"
        _save()


@app.post("/clones", status_code=201)
async def create(spec: CloneSpec):
    if spec.patch_ref is not None:
        raise HTTPException(409, "patch_ref is not supported by the advanced lab yet")
    if spec.versions != {"advanced": "v1"}:
        raise HTTPException(400, "advanced clones accept versions={'advanced': 'v1'} only")
    async with _create_lock:
        alive = [r for r in _state["clones"].values()
                 if r["status"] != CloneStatus.destroyed.value]
        if len(alive) >= MAX_CLONES:
            raise HTTPException(409, "advanced lab is at capacity")
        clone_id = f"adv-{uuid.uuid4().hex[:10]}"
        namespace = f"faultline-advanced-clone-{secrets.token_hex(4)}"
        port = CONTROL_PORT_BASE
        token = secrets.token_urlsafe(24)
        record = {
            "namespace": namespace,
            "namespace_uid": None,
            "spec": spec.model_dump(mode="json"),
            "status": CloneStatus.creating.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": time.time() + CLONE_TTL_S,
            "port": None,
            "actions": [],
            "detail": "creating",
        }
        _state["clones"][clone_id] = record
        _save()
        try:
            _preflight()
            _kubectl(["create", "-f", "-"], input_text=json.dumps({
                "apiVersion": "v1", "kind": "Namespace",
                "metadata": {"name": namespace,
                             "labels": {"faultline.dev/managed-by": "distributed-demo"}},
            }), timeout=60)
            record["namespace_uid"] = _namespace_uid(namespace)
            _save()
            secret = {
                "apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": "advanced-secrets", "namespace": namespace,
                             "labels": {"faultline.dev/managed-by": "distributed-demo"}},
                "stringData": {
                    "postgres-password": secrets.token_urlsafe(24),
                    "replication-password": secrets.token_urlsafe(24),
                    "redis-password": secrets.token_urlsafe(24),
                    "control-token": token,
                },
            }
            _kubectl(["create", "-f", "-"], input_text=json.dumps(secret))
            objects = [o for o in manifests.render(namespace) if o["kind"] != "Namespace"]
            for obj in objects:
                if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "loadgen":
                    env = obj["spec"]["template"]["spec"]["containers"][0]["env"]
                    env.append({"name": "LOAD_RPS", "value": str(spec.workload.rps)})
            _kubectl(["create", "-f", "-"], input_text=json.dumps(
                {"apiVersion": "v1", "kind": "List", "items": objects}), timeout=300)
            await asyncio.to_thread(_wait_ready, namespace)
            forward = _start_forward(namespace, port)
            _state["forwards"][clone_id] = forward
            try:
                deadline = time.monotonic() + 30
                baseline = None
                while time.monotonic() < deadline:
                    try:
                        snap = await asyncio.to_thread(
                            _http_json, f"http://{HOST}:{port}/snapshot", 5, "GET", token)
                        if _snapshot_ok(snap):
                            baseline = snap
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(1)
                if baseline is None:
                    raise LabError("clone snapshot did not reach a healthy baseline")
                await asyncio.sleep(5)
                second = await asyncio.to_thread(
                    _http_json, f"http://{HOST}:{port}/snapshot", 5, "GET", token)
                if not _snapshot_ok(second):
                    raise LabError("clone snapshot regressed during baseline observation")
                if _snapshot_activity(second) <= _snapshot_activity(baseline):
                    raise LabError("clone workload showed no activity during baseline observation")
            except Exception:
                forward.terminate()
                _state["forwards"].pop(clone_id, None)
                raise
        except Exception as exc:
            await _fail_creation(clone_id, exc)
            raise HTTPException(503, f"clone creation failed: {type(exc).__name__}") from exc
        _write_token(clone_id, token)
        record["status"] = CloneStatus.ready.value
        record["port"] = port
        record["detail"] = "infrastructure ready; clone reproduction not qualified"
        _save()
        return _to_info(clone_id, record)


@app.get("/clones/{clone_id}")
async def get(clone_id: str):
    _check_id(clone_id)
    record = _state["clones"].get(clone_id)
    if record is None:
        raise HTTPException(404, f"unknown clone {clone_id!r}")
    return _to_info(clone_id, record)


@app.post("/clones/{clone_id}/actions", status_code=201)
async def apply(clone_id: str, body: LabActionRequest):
    _check_id(clone_id)
    record = _state["clones"].get(clone_id)
    if record is None:
        raise HTTPException(404, f"unknown clone {clone_id!r}")
    remaining = record["expires_at"] - time.time()
    if record["status"] != CloneStatus.ready.value or remaining <= 0:
        raise HTTPException(409, "clone is not accepting actions")
    spec = next((a for a in LAB_CATALOG if a.id == body.action), None)
    if spec is None:
        raise HTTPException(404, f"unknown lab action {body.action!r}")
    if body.ttl_s > spec.max_ttl_s or body.ttl_s > remaining:
        raise HTTPException(400, "ttl_s exceeds the action or clone lifetime bound")
    token = _token_path(clone_id).read_text().strip()
    try:
        result = await asyncio.to_thread(
            _http_json, f"http://{HOST}:{record['port']}/lab/actions", 10, "POST",
            token,
            {"action": body.action, "params": body.params, "ttl_s": body.ttl_s})
    except Exception as exc:
        raise HTTPException(502, f"clone control refused the action: {type(exc).__name__}") from exc
    handle = LabActionHandle(
        action_id=result["action_id"], clone_id=clone_id, action=body.action,
        params=body.params, ttl_s=body.ttl_s, status=ActionStatus.active)
    record["actions"].append(handle.model_dump(mode="json"))
    _save()
    return handle.model_dump(mode="json")


@app.get("/clones/{clone_id}/actions")
async def actions(clone_id: str):
    _check_id(clone_id)
    record = _state["clones"].get(clone_id)
    if record is None:
        raise HTTPException(404, f"unknown clone {clone_id!r}")
    entries = record.get("actions", [])
    if record["status"] == CloneStatus.ready.value and record.get("port"):
        try:
            token = _token_path(clone_id).read_text().strip()
        except OSError:
            token = None
        if token:
            changed = False
            for entry in entries:
                if entry["status"] != ActionStatus.active.value:
                    continue
                try:
                    result = await asyncio.to_thread(
                        _http_json,
                        f"http://{HOST}:{record['port']}/lab/actions/{entry['action_id']}",
                        5, "GET", token)
                    if result.get("status") and result["status"] != entry["status"]:
                        entry["status"] = result["status"]
                        changed = True
                except Exception:
                    pass
            if changed:
                _save()
    return entries


@app.delete("/clones/{clone_id}/actions/{action_id}")
async def undo(clone_id: str, action_id: str):
    _check_id(clone_id)
    record = _state["clones"].get(clone_id)
    if record is None:
        raise HTTPException(404, f"unknown clone {clone_id!r}")
    entry = next((a for a in record["actions"] if a["action_id"] == action_id), None)
    if entry is None:
        raise HTTPException(404, f"unknown action {action_id!r}")
    if entry["status"] == ActionStatus.active.value:
        try:
            token = _token_path(clone_id).read_text().strip()
            result = await asyncio.to_thread(
                _http_json, f"http://{HOST}:{record['port']}/lab/actions/{action_id}",
                10, "DELETE", token)
            entry["status"] = result.get("status", ActionStatus.undone.value)
        except Exception:
            entry["status"] = ActionStatus.failed.value
        _save()
    return entry


class WorkloadBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    rps: float = Field(gt=0, le=200)


@app.post("/clones/{clone_id}/workload")
async def set_workload(clone_id: str, body: WorkloadBody):
    _check_id(clone_id)
    record = _state["clones"].get(clone_id)
    if record is None:
        raise HTTPException(404, f"unknown clone {clone_id!r}")
    if record["status"] != CloneStatus.ready.value \
            or time.time() >= record["expires_at"]:
        raise HTTPException(409, "clone is not ready")
    try:
        namespace = _owned_namespace(record)
    except LabError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        if _namespace_uid(namespace) != record["namespace_uid"]:
            raise HTTPException(409, "namespace UID mismatch; refusing to mutate")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"namespace ownership unverifiable: {type(exc).__name__}") from exc
    try:
        dep = json.loads(_kubectl(
            ["get", "deployment", "loadgen", "-n", namespace, "-o", "json"]))
    except Exception as exc:
        raise HTTPException(502, f"loadgen deployment unreadable: {type(exc).__name__}") from exc
    env = dep["spec"]["template"]["spec"]["containers"][0].get("env", [])
    index = next((i for i, e in enumerate(env) if e.get("name") == "LOAD_RPS"), None)
    path = "/spec/template/spec/containers/0/env"
    ops = [{"op": "test", "path": "/metadata/resourceVersion",
            "value": dep["metadata"]["resourceVersion"]}]
    ops.append({"op": "add", "path": f"{path}/-",
                "value": {"name": "LOAD_RPS", "value": str(body.rps)}}
               if index is None else
               {"op": "replace", "path": f"{path}/{index}/value", "value": str(body.rps)})
    try:
        _kubectl(["patch", "deployment", "loadgen", "-n", namespace,
                  "--type", "json", "-p", json.dumps(ops)])
        _kubectl(["rollout", "status", "deployment/loadgen", "-n", namespace,
                  "--timeout=120s"], timeout=140)
    except Exception as exc:
        raise HTTPException(502, f"loadgen update failed: {type(exc).__name__}") from exc
    record["spec"]["workload"]["rps"] = body.rps
    _save()
    return _to_info(clone_id, record)


@app.post("/clones/{clone_id}/reset")
async def reset(clone_id: str):
    _check_id(clone_id)
    if clone_id not in _state["clones"]:
        raise HTTPException(404, f"unknown clone {clone_id!r}")
    raise HTTPException(503, "advanced reset measurement gate pending qualification")


async def _destroy(clone_id: str) -> None:
    record = _state["clones"].get(clone_id)
    if record is None or record["status"] in (CloneStatus.destroyed.value, "quarantined"):
        return
    try:
        namespace = _owned_namespace(record)
    except LabError:
        record["status"] = "quarantined"
        record["detail"] = "namespace is not a clone namespace; refusing to delete"
        _save()
        return
    try:
        out = _kubectl(["get", "namespace", namespace, "--ignore-not-found", "-o", "json"])
    except Exception:
        record["status"] = "cleanup_pending"
        _save()
        return
    if out.strip():
        try:
            uid = json.loads(out)["metadata"]["uid"]
        except Exception:
            record["status"] = "cleanup_pending"
            _save()
            return
        if not record.get("namespace_uid") or uid != record["namespace_uid"]:
            record["status"] = "quarantined"
            record["detail"] = "namespace UID unknown or mismatched; refusing to delete"
            _save()
            return
        try:
            _kubectl(["delete", "namespace", namespace, "--wait=false"], timeout=60)
        except Exception:
            record["status"] = "cleanup_pending"
            _save()
            return
        deadline = time.monotonic() + DESTROY_POLL_S
        while time.monotonic() < deadline:
            try:
                gone = not _kubectl(
                    ["get", "namespace", namespace, "--ignore-not-found"]).strip()
            except Exception:
                gone = False
            if gone:
                break
            await asyncio.sleep(2)
        else:
            record["status"] = "cleanup_pending"
            record["detail"] = "namespace still terminating"
            _save()
            return
    proc = _state["forwards"].pop(clone_id, None)
    if proc is not None:
        proc.terminate()
    record["status"] = CloneStatus.destroyed.value
    record["port"] = None
    record["detail"] = "destroyed"
    _drop_token(clone_id)
    _save()


@app.delete("/clones/{clone_id}")
async def destroy(clone_id: str):
    _check_id(clone_id)
    record = _state["clones"].get(clone_id)
    if record is None:
        raise HTTPException(404, f"unknown clone {clone_id!r}")
    await _destroy(clone_id)
    if record["status"] == "quarantined":
        raise HTTPException(409, record["detail"])
    return _to_info(clone_id, record)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
