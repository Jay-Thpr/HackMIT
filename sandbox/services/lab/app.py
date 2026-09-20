"""Clone lab manager (:9910), C6. Serves disposable, clean replicas of the sandbox where
investigators run experiments too aggressive for production.

Runs on the host (it drives `docker compose`), next to the production stack:

    cd sandbox && uv run uvicorn services.lab.app:app --host 127.0.0.1 --port 9910

A clone is the production compose file started under its own project `faultline-clone-<slot>`
(own network, host ports shifted by 1000*slot) WITHOUT the fault controller. It is built only
from the CloneSpec: service versions, retry policy, workload rate and an optional patch ref.
Nothing from production's fault controller, io_profile or DB contents is copied, and :9900 is
not routable from a clone's network (separate compose networks).

Lab actions change the clone's physical world the same way the hidden controller changes
production's (io_profile cost, docker cpu limits, container stop/start) but only ever on a
project this manager created. Every action has a ttl and is reverted here when it expires.
"""

import asyncio
import json
import logging
import math
import os
import signal
import time
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from faultline_contracts.clone import (LAB_CATALOG, MAX_CLONES, CloneEndpoints, CloneInfo, CloneSpec, CloneStatus,
                                       LabActionHandle, LabActionRequest, WorkloadSpec)
from faultline_contracts.common import utcnow
from faultline_contracts.levers import ActionStatus
from services.common import probe

log = logging.getLogger("lab")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

SANDBOX_DIR = Path(__file__).resolve().parents[2]
COMPOSE_FILE = SANDBOX_DIR / "docker-compose.yml"
CLONE_OVERRIDE = SANDBOX_DIR / "clone.override.yml"  # no masquerade: clones can't pose as host traffic
PRODUCTION_PROJECT = "faultline-sandbox"
CLONE_PROJECT_PREFIX = "faultline-clone-"
CLONE_SERVICES = ["db-primary", "db-standby", "payments", "orders", "envoy", "loadgen", "control",
                  "otel-collector"]  # never faultctl
LAB_SERVICES = {"orders", "orders-v2", "payments"}
MAX_CLONES_CFG = int(os.environ.get("LAB_MAX_CLONES", "2"))
MAX_LIFETIME_S = float(os.environ.get("LAB_CLONE_MAX_LIFETIME_S", "3600"))
if not 1 <= MAX_CLONES_CFG <= MAX_CLONES:
    raise ValueError(f"LAB_MAX_CLONES must be between 1 and {MAX_CLONES}")
if not math.isfinite(MAX_LIFETIME_S) or MAX_LIFETIME_S <= 0:
    raise ValueError("LAB_CLONE_MAX_LIFETIME_S must be finite and positive")
HOST = os.environ.get("LAB_CLONE_HOST", "127.0.0.1")
DB_BASE_MS = float(os.environ.get("DB_BASE_MS", "38"))
DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "4"))
READY_TIMEOUT_S = float(os.environ.get("LAB_READY_TIMEOUT_S", "120"))
RESET_TIMEOUT_S = float(os.environ.get("RESET_TIMEOUT_S", "120"))
TOKEN = {"X-Sandbox-Token": os.environ.get("SANDBOX_TOKEN", "sandbox-internal")}
BASE_PORTS = {"gateway": 8080, "orders": 8101, "payments": 8102, "loadgen": 8103, "orders-v2": 8104, "control": 9901}
CATALOG = {a.id: a for a in LAB_CATALOG}

http = httpx.AsyncClient(timeout=5.0)


class LabFailure(Exception):
    """Docker or a clone service failed; reported as 503."""


# ---- docker plumbing -----------------------------------------------------------------------
async def _terminate_process(proc) -> None:
    with suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    await proc.communicate()


async def run(*args: str, env: dict[str, str] | None = None, timeout_s: float = 300) -> str:
    proc = await asyncio.create_subprocess_exec(*args, cwd=SANDBOX_DIR, env={**os.environ, **(env or {})},
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                start_new_session=os.name == "posix")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except asyncio.TimeoutError:
        await _terminate_process(proc)
        raise LabFailure(f"{' '.join(args[:4])} timed out after {timeout_s:.0f}s")
    except asyncio.CancelledError:
        await _terminate_process(proc)
        raise
    if proc.returncode:
        raise LabFailure(f"{' '.join(args[:4])} failed: {err.decode(errors='replace').strip()[-600:]}")
    return out.decode()


def _assert_clone_project(project: str) -> None:
    if not project.startswith(CLONE_PROJECT_PREFIX) or project == PRODUCTION_PROJECT:
        raise LabFailure(f"refusing to touch project {project!r}: not a clone")


class Clone:
    def __init__(self, clone_id: str, slot: int, spec: CloneSpec) -> None:
        self.clone_id, self.slot, self.spec = clone_id, slot, spec
        self.project = f"{CLONE_PROJECT_PREFIX}{slot}"
        self.status = CloneStatus.creating
        self.created_at = utcnow()
        self.expires_at = self.created_at + timedelta(seconds=MAX_LIFETIME_S)
        self.deadline = time.monotonic() + MAX_LIFETIME_S
        self.cleanup_retry_at = 0.0
        self.detail: str | None = None
        self.actions: dict[str, LabActionHandle] = {}
        self.db_extra: dict[str, float] = {}  # action_id -> extra_ms contribution on the primary
        self.cpu_orig: dict[str, int] = {}  # service -> original NanoCpus
        self.lock = asyncio.Lock()
        self.ports = {k: v + 1000 * slot for k, v in BASE_PORTS.items()}

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    # -- env / compose ---------------------------------------------------------------------
    def env(self) -> dict[str, str]:
        e = {
            "LOAD_RPS": str(self.spec.workload.rps),
            "ORDERS_MAX_RETRIES": str(self.spec.retry_policy.max_retries),
            "ORDERS_ATTEMPT_TIMEOUT_MS": str(self.spec.retry_policy.timeout_ms),
            "ORDERS_V2_IMAGE": f"faultline-clone-orders-v2:{self.slot}",
            "PORT_GATEWAY": str(self.ports["gateway"]), "PORT_ORDERS": str(self.ports["orders"]),
            "PORT_PAYMENTS": str(self.ports["payments"]), "PORT_LOADGEN": str(self.ports["loadgen"]),
            "PORT_ORDERS_V2": str(self.ports["orders-v2"]), "PORT_CONTROL": str(self.ports["control"]),
            "OTEL_DEPLOYMENT_ENVIRONMENT": f"clone-{self.slot}",
            # Clone collectors tee structured records to a file so the fairness check reads emitted
            # records, not process logs; production stays Elastic-only.
            "FAULTLINE_OTEL_TEE": "1",
            "FAULTLINE_OTEL_TEE_DIR": str(self._tee_dir()),
        }
        if self.spec.patch_ref:
            e["ORDERS_V2_CONTEXT"] = str(Path(self.spec.patch_ref).expanduser().resolve())
        return e

    def _tee_dir(self) -> Path:
        d = SANDBOX_DIR / ".otel-records" / self.project
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o777)  # the collector writes as uid 10001
        return d

    async def compose(self, *args: str, timeout_s: float = 300) -> str:
        _assert_clone_project(self.project)
        return await run("docker", "compose", "-p", self.project, "-f", str(COMPOSE_FILE), "-f", str(CLONE_OVERRIDE),
                         "--profile", "canary", *args, env=self.env(), timeout_s=timeout_s)

    async def container(self, service: str) -> str:
        """Container id of a clone service, verified to belong to this clone's project."""
        _assert_clone_project(self.project)
        out = await run("docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}",
                        "--filter", f"label=com.docker.compose.service={service}")
        ids = out.split()
        if not ids:
            raise LabFailure(f"no container for {service} in {self.project}")
        return ids[0]

    async def psql(self, service: str, sql: str) -> str:
        return await run("docker", "exec", await self.container(service), "psql", "-U", "app", "-d", "shop", "-tAc",
                         sql, timeout_s=15)

    async def set_db_extra(self) -> None:
        extra = int(round(sum(self.db_extra.values())))
        await self.psql("db-primary", f"UPDATE io_profile SET base_ms = {int(round(DB_BASE_MS))}, extra_ms = {extra} WHERE id = 1")

    async def init_db_cost(self) -> None:
        for svc in ("db-primary", "db-standby"):
            for _ in range(30):
                try:
                    await self.psql(svc, f"UPDATE io_profile SET base_ms = {int(round(DB_BASE_MS))}, extra_ms = 0 WHERE id = 1")
                    break
                except LabFailure as e:
                    log.info("%s: %s not ready: %s", self.clone_id, svc, str(e)[-120:])
                    await asyncio.sleep(1)
            else:
                raise LabFailure(f"{svc} never accepted connections")

    # -- urls -------------------------------------------------------------------------------
    def url(self, name: str) -> str:
        return f"http://{HOST}:{self.ports[name]}"

    def endpoints(self) -> CloneEndpoints:
        stats = {s: self.url(s) for s in ("orders", "payments", "loadgen")}
        if self.spec.patch_ref:
            stats["orders-v2"] = self.url("orders-v2")
        return CloneEndpoints(gateway_url=self.url("gateway"), control_url=self.url("control"), stats_urls=stats)

    def info(self) -> CloneInfo:
        alive = self.status in (CloneStatus.ready, CloneStatus.resetting)
        return CloneInfo(clone_id=self.clone_id, status=self.status, spec=self.spec, created_at=self.created_at,
                         endpoints=self.endpoints() if alive else None,
                         active_actions=[h for h in self.actions.values() if h.status == ActionStatus.active],
                         detail=self.detail)

    # -- health -----------------------------------------------------------------------------
    async def snapshot(self) -> probe.Snap:
        names = ("orders", "payments", "loadgen")
        rs = await asyncio.gather(*(http.get(f"{self.url(n)}/stats") for n in names))
        return {n: r.json() for n, r in zip(names, rs)}

    async def wait_until(self, pred, window_s: float, timeout_s: float) -> dict[str, Any] | None:
        snaps: list[tuple[float, probe.Snap]] = []
        deadline, last = time.monotonic() + timeout_s, None
        while time.monotonic() < deadline:
            try:
                snaps.append((time.monotonic(), await self.snapshot()))
            except (httpx.HTTPError, ValueError, KeyError) as e:
                log.info("%s: probe failed: %s", self.clone_id, str(e)[:120])
            while len(snaps) > 1 and snaps[-1][0] - snaps[1][0] >= window_s:
                snaps.pop(0)
            if len(snaps) > 1 and snaps[-1][0] - snaps[0][0] >= window_s:
                last = probe.row(snaps[0][1], snaps[-1][1])
                if pred(last):
                    return last
            await asyncio.sleep(0.5)
        log.warning("%s: wait_until timed out; last window: %s", self.clone_id, last)
        return None

    def healthy(self, r: dict[str, Any]) -> bool:
        return probe.is_healthy(r, self.spec.retry_policy.timeout_ms)

    def drained(self, r: dict[str, Any]) -> bool:
        return (r["pool_waiting"] or 0) == 0 and r["db_p99_ms"] is not None \
            and r["db_p99_ms"] < self.spec.retry_policy.timeout_ms / 2 and (r["ok_ratio"] or 0) >= 0.98

    # -- orders override (retry_policy action + drain) ----------------------------------------
    def orders_urls(self) -> list[str]:
        return [self.url("orders")] + ([self.url("orders-v2")] if self.spec.patch_ref else [])

    async def orders_override(self, body: dict[str, Any] | None) -> None:
        for i, u in enumerate(self.orders_urls()):
            try:
                if body is None:
                    (await http.delete(f"{u}/internal/retry_override", headers=TOKEN)).raise_for_status()
                else:
                    (await http.post(f"{u}/internal/retry_override", json=body, headers=TOKEN)).raise_for_status()
            except httpx.HTTPError as e:
                if i == 0:
                    raise LabFailure(f"orders unreachable: {e}")

    # -- lifecycle -----------------------------------------------------------------------------
    async def start(self) -> None:
        _assert_clone_project(self.project)
        for record_file in self._tee_dir().glob("records*.jsonl"):
            record_file.unlink()
        if self.spec.patch_ref:
            await self.compose("build", "orders-v2", timeout_s=600)
        await self.compose("up", "-d", "--no-build", "--wait", *CLONE_SERVICES, timeout_s=240)
        if self.spec.patch_ref:
            await self.compose("up", "-d", "--no-build", "orders-v2", timeout_s=120)
        await self.init_db_cost()

    async def verify_ready(self, timeout_s: float) -> None:
        row = await self.wait_until(self.healthy, 5.0, timeout_s)
        if row is None:
            raise LabFailure("clone did not reach a healthy baseline")
        log.info("%s ready: retry %.2f ok %.2f db_p99 %.0f", self.clone_id, row["retry_ratio"], row["ok_ratio"],
                 row["db_p99_ms"])

    async def reset(self) -> None:
        t0 = time.monotonic()
        for h in list(self.actions.values()):
            if h.status == ActionStatus.active:
                await self.undo(h, ActionStatus.undone)
        self.db_extra.clear()
        await self.set_db_extra()
        await self.restore_all_cpus()
        await self.compose("start", *CLONE_SERVICES, *(["orders-v2"] if self.spec.patch_ref else []), timeout_s=120)
        for path in ("retry_override", "shed", "db/failover", "canary"):
            try:
                (await http.delete(f"{self.url('control')}/admin/{path}")).raise_for_status()
            except httpx.HTTPError as e:
                raise LabFailure(f"clone control unreachable: {e}")
        await self.apply_workload(self.spec.workload)
        for round_ in range(1, 4):
            remaining = RESET_TIMEOUT_S - (time.monotonic() - t0)
            await self.orders_override({"max_retries": 0, "ttl_s": max(5.0, remaining)})
            if await self.wait_until(self.drained, 2.0, remaining) is None:
                break
            await self.orders_override(None)
            remaining = RESET_TIMEOUT_S - (time.monotonic() - t0)
            if await self.wait_until(self.healthy, 5.0, min(15.0, remaining)) is not None:
                log.info("%s reset complete in %.1fs (round %d)", self.clone_id, time.monotonic() - t0, round_)
                return
        await self.orders_override(None)
        raise LabFailure("clone did not return to a healthy baseline")

    async def apply_workload(self, w: WorkloadSpec) -> None:
        try:
            (await http.post(f"{self.url('loadgen')}/rate", json={"rps": w.rps})).raise_for_status()
        except httpx.HTTPError as e:
            raise LabFailure(f"clone loadgen unreachable: {e}")

    async def teardown(self) -> None:
        await self.compose("down", "--remove-orphans", "-t", "5", timeout_s=180)

    # -- lab actions -----------------------------------------------------------------------------
    async def apply(self, h: LabActionHandle) -> None:
        a, p = h.action, h.params
        if a == "retry_policy":
            body = {k: p[k] for k in ("max_retries", "timeout_ms") if k in p} or self.spec.retry_policy.model_dump()
            await self.orders_override({**body, "ttl_s": h.ttl_s})
        elif a == "db_latency":
            self.db_extra[h.action_id] = float(p["extra_ms"])
            await self.set_db_extra()
        elif a == "db_capacity":
            self.db_extra[h.action_id] = max(0.0, DB_POOL_SIZE * 1000.0 / float(p["capacity_qps"]) - DB_BASE_MS)
            await self.set_db_extra()
        elif a == "cpu_limit":
            cid = await self.container(p["service"])
            if p["service"] not in self.cpu_orig:
                nano = (await run("docker", "inspect", "-f", "{{.HostConfig.NanoCpus}}", cid)).strip()
                self.cpu_orig[p["service"]] = int(nano or 0)
            await run("docker", "update", "--cpus", str(p["cpus"]), cid)
        elif a == "service_kill":
            await self.compose("stop", "-t", "2", p["service"], timeout_s=60)
        elif a == "service_restart":
            await self.compose("restart", "-t", "2", p["service"], timeout_s=120)

    async def undo(self, h: LabActionHandle, status: ActionStatus) -> None:
        a, p = h.action, h.params
        h.status = status
        if a == "retry_policy":
            await self.orders_override(None)
        elif a in ("db_latency", "db_capacity"):
            self.db_extra.pop(h.action_id, None)
            await self.set_db_extra()
        elif a == "cpu_limit":
            await self.restore_cpus(p["service"])
        elif a == "service_kill":
            await self.compose("start", p["service"], timeout_s=60)

    async def restore_cpus(self, service: str) -> None:
        nano = self.cpu_orig.pop(service, None)
        if nano is None:
            return
        cpus = nano / 1e9 if nano else float((await run("docker", "info", "-f", "{{.NCPU}}")).strip())
        await run("docker", "update", "--cpus", f"{cpus:g}", await self.container(service))

    async def restore_all_cpus(self) -> None:
        for svc in list(self.cpu_orig):
            await self.restore_cpus(svc)


# ---- registry ------------------------------------------------------------------------------
clones: dict[str, Clone] = {}
_create_lock = asyncio.Lock()
_seq = 0


def _alive() -> list[Clone]:
    return [c for c in clones.values() if c.status != CloneStatus.destroyed]


def _get(clone_id: str) -> Clone:
    c = clones.get(clone_id)
    if c is None:
        raise HTTPException(status_code=404, detail=f"unknown clone {clone_id!r}")
    return c


def _ready(c: Clone) -> None:
    if c.status != CloneStatus.ready:
        raise HTTPException(status_code=409, detail=f"clone {c.clone_id} is {c.status.value}, not ready")
    if c.remaining_s <= 0:
        raise HTTPException(status_code=409, detail=f"clone {c.clone_id} lifetime expired")


async def _destroy(c: Clone, reason: str | None = None) -> None:
    if c.status == CloneStatus.destroyed:
        return
    c.status = CloneStatus.failed
    c.detail = reason or "clone cleanup in progress"
    try:
        await c.teardown()
    except LabFailure as exc:
        c.status = CloneStatus.failed
        c.detail = f"cleanup pending: {exc}"
        c.cleanup_retry_at = time.monotonic() + 30
        log.warning("%s: %s", c.clone_id, c.detail)
        raise
    for h in c.actions.values():
        if h.status == ActionStatus.active:
            h.status = ActionStatus.undone
    c.status = CloneStatus.destroyed
    c.detail = reason
    log.info("%s destroyed%s", c.clone_id, f": {reason}" if reason else "")


def _cleanup_due(c: Clone) -> bool:
    return (
        c.status != CloneStatus.destroyed
        and (c.status == CloneStatus.failed or c.remaining_s <= 0)
        and time.monotonic() >= c.cleanup_retry_at
    )


async def _reap_clone(c: Clone) -> None:
    async with c.lock:
        if not _cleanup_due(c):
            return
        reason = "clone lifetime expired" if c.remaining_s <= 0 else c.detail
        try:
            await _destroy(c, reason)
        except LabFailure:
            pass


async def _lease_loop() -> None:
    pending: dict[str, asyncio.Task] = {}
    try:
        while True:
            for clone_id, task in list(pending.items()):
                if task.done():
                    del pending[clone_id]
                    try:
                        task.result()
                    except Exception:
                        log.exception("%s: clone cleanup task failed", clone_id)
            for c in list(clones.values()):
                if c.clone_id not in pending and not c.lock.locked() and _cleanup_due(c):
                    pending[c.clone_id] = asyncio.create_task(_reap_clone(c))
            await asyncio.sleep(1)
    finally:
        for task in pending.values():
            task.cancel()
        await asyncio.gather(*pending.values(), return_exceptions=True)


async def _expire_actions(c: Clone) -> None:
    if c.lock.locked():
        return
    now = utcnow()
    due = [h for h in c.actions.values() if h.status == ActionStatus.active and now >= h.expires_at]
    if not due:
        return
    async with c.lock:
        if c.status != CloneStatus.ready:
            return
        for h in due:
            if h.status != ActionStatus.active:
                continue
            try:
                log.info("%s: action %s %s ttl expired, reverting", c.clone_id, h.action, h.action_id)
                await c.undo(h, ActionStatus.expired)
            except LabFailure as e:
                log.warning("%s: revert %s failed: %s", c.clone_id, h.action, e)
                h.status = ActionStatus.failed


async def _expiry_loop() -> None:
    while True:
        for c in list(clones.values()):
            if c.status != CloneStatus.ready:
                continue
            await _expire_actions(c)
        await asyncio.sleep(0.5)


def _validate_params(action: str, params: dict[str, Any]) -> None:
    schema = CATALOG[action].params_schema
    props = schema["properties"]
    extra = set(params) - set(props)
    if extra:
        raise HTTPException(status_code=400, detail=f"unknown params for {action}: {sorted(extra)}")
    missing = set(schema.get("required", [])) - set(params)
    if missing:
        raise HTTPException(status_code=400, detail=f"missing params for {action}: {sorted(missing)}")
    for k, v in params.items():
        s = props[k]
        ok = {"integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
              "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
              "string": lambda x: isinstance(x, str)}[s["type"]](v)
        if ok and "enum" in s:
            ok = v in s["enum"]
        if ok and "minimum" in s:
            ok = v >= s["minimum"]
        if ok and "exclusiveMinimum" in s:
            ok = v > s["exclusiveMinimum"]
        if ok and "maximum" in s:
            ok = v <= s["maximum"]
        if not ok:
            raise HTTPException(status_code=400, detail=f"bad value for {action}.{k}: {v!r}")


async def _sweep_orphans() -> None:
    """Clones from a previous manager run are disposable state we no longer track: remove them."""
    try:
        out = await run("docker", "compose", "ls", "--all", "--format", "json")
    except LabFailure as e:
        log.warning("compose ls failed: %s", e)
        return
    for p in json.loads(out or "[]"):
        name = p.get("Name", "")
        if name.startswith(CLONE_PROJECT_PREFIX) and name != PRODUCTION_PROJECT:
            log.info("removing orphaned clone project %s", name)
            try:
                await run("docker", "compose", "-p", name, "down", "--remove-orphans", "-t", "5", timeout_s=180)
            except LabFailure as e:
                log.warning("orphan sweep of %s failed: %s", name, e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await _sweep_orphans()
    expiry = asyncio.create_task(_expiry_loop())
    lease = asyncio.create_task(_lease_loop())
    try:
        yield
    finally:
        expiry.cancel()
        lease.cancel()
        await asyncio.gather(expiry, lease, return_exceptions=True)


app = FastAPI(lifespan=lifespan)


@app.exception_handler(LabFailure)
async def _lab_failure(_req, exc: LabFailure):
    return JSONResponse({"detail": str(exc)}, status_code=503)


def _json(m) -> JSONResponse:
    return JSONResponse(m.model_dump(mode="json"))


# ---- API -------------------------------------------------------------------------------------
@app.get("/lab/catalog")
async def catalog():
    return [a.model_dump(mode="json") for a in LAB_CATALOG]


@app.get("/clones")
async def list_clones():
    return [c.info().model_dump(mode="json") for c in clones.values()]


@app.post("/clones")
async def create_clone(spec: CloneSpec):
    global _seq
    if spec.patch_ref and not (Path(spec.patch_ref).expanduser() / "sandbox" / "Dockerfile").exists():
        raise HTTPException(status_code=400, detail="patch_ref must be a checkout root containing sandbox/Dockerfile")
    if set(spec.versions) - {"orders", "payments"}:
        raise HTTPException(status_code=400, detail="versions may name only orders and payments")
    async with _create_lock:
        alive = _alive()
        if len(alive) >= MAX_CLONES_CFG:
            raise HTTPException(status_code=409, detail=f"at capacity: {MAX_CLONES_CFG} clones alive")
        slot = min(set(range(1, MAX_CLONES_CFG + 1)) - {c.slot for c in alive})
        _seq += 1
        c = Clone(f"{spec.name}-{_seq}", slot, spec)
        clones[c.clone_id] = c
    log.info("creating %s as project %s (ports %s)", c.clone_id, c.project, c.ports)
    async with c.lock:
        try:
            async with asyncio.timeout(c.remaining_s):
                await c.start()
                await c.verify_ready(READY_TIMEOUT_S)
            c.status = CloneStatus.ready
        except (LabFailure, TimeoutError) as exc:
            failure = exc if isinstance(exc, LabFailure) else LabFailure("clone lifetime expired during provisioning")
            c.status, c.detail = CloneStatus.failed, str(failure)
            log.error("%s failed: %s", c.clone_id, failure)
            try:
                await _destroy(c, str(failure))
            except LabFailure:
                pass
            raise failure
        except asyncio.CancelledError:
            c.status, c.detail = CloneStatus.failed, "clone provisioning cancelled"
            raise
    return _json(c.info())


@app.get("/clones/{clone_id}")
async def get_clone(clone_id: str):
    return _json(_get(clone_id).info())


@app.post("/clones/{clone_id}/reset")
async def reset_clone(clone_id: str):
    c = _get(clone_id)
    async with c.lock:
        _ready(c)
        c.status, c.detail = CloneStatus.resetting, None
        try:
            async with asyncio.timeout(c.remaining_s):
                await c.reset()
            c.status = CloneStatus.ready
        except TimeoutError:
            c.status, c.detail = CloneStatus.failed, "clone lifetime expired during reset"
            raise LabFailure("clone lifetime expired during reset")
        except LabFailure as e:
            c.status, c.detail = CloneStatus.failed, str(e)
            raise
        except asyncio.CancelledError:
            c.status, c.detail = CloneStatus.failed, "clone reset cancelled"
            raise
    return _json(c.info())


@app.delete("/clones/{clone_id}")
async def destroy_clone(clone_id: str):
    c = _get(clone_id)
    async with c.lock:
        await _destroy(c)
    return _json(c.info())


@app.post("/clones/{clone_id}/workload")
async def set_workload(clone_id: str, w: WorkloadSpec):
    c = _get(clone_id)
    async with c.lock:
        _ready(c)
        await c.apply_workload(w)
        c.spec = c.spec.model_copy(update={"workload": w})
    return _json(c.info())


@app.post("/clones/{clone_id}/actions")
async def apply_action(clone_id: str, req: LabActionRequest):
    c = _get(clone_id)
    spec = CATALOG.get(req.action)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"unknown lab action {req.action!r}")
    if not 1 <= req.ttl_s <= spec.max_ttl_s:
        raise HTTPException(status_code=400, detail=f"ttl_s must be in [1, {spec.max_ttl_s}]")
    _validate_params(req.action, req.params)
    if req.params.get("service") == "orders-v2" and not c.spec.patch_ref:
        raise HTTPException(status_code=409, detail="this clone has no orders-v2 (no patch_ref)")
    async with c.lock:
        _ready(c)
        if req.ttl_s > c.remaining_s:
            raise HTTPException(status_code=409, detail="action ttl_s exceeds remaining clone lifetime")
        h = LabActionHandle(action_id=f"a{len(c.actions) + 1}", clone_id=c.clone_id, action=req.action,
                            params=req.params, ttl_s=req.ttl_s)
        c.actions[h.action_id] = h
        try:
            await c.apply(h)
        except LabFailure:
            h.status = ActionStatus.failed
            raise
        if not spec.reversible:
            h.status = ActionStatus.expired  # one-shot: done as soon as it ran
        log.info("%s: action %s %s applied %s for %ss", c.clone_id, h.action, h.action_id, h.params, h.ttl_s)
    return _json(h)


@app.delete("/clones/{clone_id}/actions/{action_id}")
async def undo_action(clone_id: str, action_id: str):
    c = _get(clone_id)
    h = c.actions.get(action_id)
    if h is None:
        raise HTTPException(status_code=404, detail=f"unknown action {action_id!r}")
    async with c.lock:
        if h.status == ActionStatus.active:
            _ready(c)
            await c.undo(h, ActionStatus.undone)
            log.info("%s: action %s %s undone", c.clone_id, h.action, h.action_id)
    return _json(h)


@app.get("/clones/{clone_id}/actions")
async def list_actions(clone_id: str):
    return [h.model_dump(mode="json") for h in _get(clone_id).actions.values()]


@app.get("/healthz")
async def healthz():
    return {
        "ok": True, "clones_alive": len(_alive()), "max_clones": MAX_CLONES_CFG,
        "clone_max_lifetime_s": MAX_LIFETIME_S,
        "clones": [
            {"clone_id": c.clone_id, "status": c.status.value,
             "expires_at": c.expires_at.isoformat(), "remaining_s": c.remaining_s,
             "cleanup_pending": c.status == CloneStatus.failed}
            for c in _alive()
        ],
    }
