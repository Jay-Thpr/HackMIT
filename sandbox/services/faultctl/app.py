"""Hidden fault controller (:9900), C5. Benchmark/demo only; never reachable from Faultline.

It changes physical reality and nothing else. It never sets metrics or world flags inside
the app; the app just experiences a slower DB or a starved CPU.

  storm       transient: the primary DB's per-query cost rises by delay_ms for duration_s,
              then returns to normal. Any storm that persists afterwards is the retry
              feedback loop sustaining itself.
  degrade_db  persistent: the primary DB's per-query cost rises so that its capacity through
              the Payments pool is capacity_qps. The standby is untouched, so failover heals.
  cpu_starve  persistent: `docker update --cpus` on a service container.
  reset       clears all of the above, reverts every lever, drains queues and returns only
              once the system is back at a healthy baseline.

The DB cost lives in table io_profile inside each Postgres; process_payment() reads it.
Capacity through the primary pool = DB_POOL_SIZE / (DB_BASE_MS + extra_ms) queries/s.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from faultline_contracts.fault import CpuStarveFault, DegradeDbFault, FaultState, StormFault, World
from services.common import probe

log = logging.getLogger("faultctl")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

PRIMARY_DSN = os.environ.get("PRIMARY_ADMIN_DSN", "postgresql://app@db-primary:5432/shop")
STANDBY_DSN = os.environ.get("STANDBY_ADMIN_DSN", "postgresql://app@db-standby:5432/shop")
DB_BASE_MS = float(os.environ.get("DB_BASE_MS", "38"))
DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "4"))
ATTEMPT_TIMEOUT_MS = float(os.environ.get("ORDERS_ATTEMPT_TIMEOUT_MS", "500"))
RESET_TIMEOUT_S = float(os.environ.get("RESET_TIMEOUT_S", "120"))
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
CONTROL_URL = os.environ.get("CONTROL_URL", "http://control:8000")
URLS = {"loadgen": os.environ.get("LOADGEN_URL", "http://loadgen:8000"),
        "orders": os.environ.get("ORDERS_URL", "http://orders:8000"),
        "payments": os.environ.get("PAYMENTS_URL", "http://payments:8000")}
ORDERS_V2_URL = os.environ.get("ORDERS_V2_URL", "http://orders-v2:8000")
TOKEN = {"X-Sandbox-Token": os.environ.get("SANDBOX_TOKEN", "sandbox-internal")}

http = httpx.AsyncClient(timeout=5.0)
_lock = asyncio.Lock()
_state: dict[str, Any] = {"world": World.none, "params": {}, "started_at": None, "storm_until": None}
_storm_task: asyncio.Task | None = None
_starved: dict[str, int] = {}  # container id -> original NanoCpus


# ---- physical knobs ------------------------------------------------------------------------
async def set_db_extra(dsn: str, extra_ms: float) -> None:
    conn = await asyncpg.connect(dsn, timeout=5)
    try:
        await conn.execute("UPDATE io_profile SET base_ms = $1, extra_ms = $2 WHERE id = 1",
                           int(round(DB_BASE_MS)), int(round(extra_ms)))
    finally:
        await conn.close()


def _docker() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=DOCKER_SOCK), base_url="http://docker",
                             timeout=10.0)


async def _containers_for(service: str) -> list[str]:
    async with _docker() as d:
        me = (await d.get(f"/containers/{os.environ.get('HOSTNAME', '')}/json")).json()
        project = me["Config"]["Labels"]["com.docker.compose.project"]
        flt = f'{{"label":["com.docker.compose.project={project}","com.docker.compose.service={service}"]}}'
        return [c["Id"] for c in (await d.get("/containers/json", params={"filters": flt})).json()]


async def set_cpus(service: str, cpus: float) -> None:
    ids = await _containers_for(service)
    if not ids:
        raise HTTPException(status_code=404, detail=f"no running container for service {service!r}")
    async with _docker() as d:
        for cid in ids:
            if cid not in _starved:
                _starved[cid] = (await d.get(f"/containers/{cid}/json")).json()["HostConfig"].get("NanoCpus", 0)
            (await d.post(f"/containers/{cid}/update", json={"NanoCpus": int(cpus * 1e9)})).raise_for_status()


async def restore_cpus() -> None:
    """Put back each starved container's original limit. Docker ignores NanoCpus=0 on update,
    so a container that had no limit gets one equal to the host's CPU count (unlimited in effect)."""
    if not _starved:
        return
    async with _docker() as d:
        ncpu = (await d.get("/info")).json()["NCPU"]
        for cid, nano in list(_starved.items()):
            r = await d.post(f"/containers/{cid}/update", json={"NanoCpus": nano or int(ncpu * 1e9)})
            if r.status_code == 404:  # container is gone; its replacement starts unstarved
                _starved.pop(cid)
                continue
            r.raise_for_status()
            _starved.pop(cid)


async def _clear_physical() -> None:
    global _storm_task
    if _storm_task and not _storm_task.done():
        _storm_task.cancel()
    _storm_task = None
    _state["storm_until"] = None
    await set_db_extra(PRIMARY_DSN, 0)
    await set_db_extra(STANDBY_DSN, 0)
    await restore_cpus()


# ---- state ---------------------------------------------------------------------------------
def fault_state() -> FaultState:
    w = _state["world"]
    if w == World.storm:
        active = _state["storm_until"] is not None and time.monotonic() < _state["storm_until"]
    else:
        active = w != World.none
    return FaultState(world=w, active=active, params=_state["params"], started_at=_state["started_at"])


def _set_world(world: World, params: dict[str, Any]) -> None:
    _state.update(world=world, params=params, started_at=datetime.now(timezone.utc))


def _resp() -> JSONResponse:
    return JSONResponse(fault_state().model_dump(mode="json"))


# ---- health probing (for reset) ------------------------------------------------------------
async def snapshot() -> probe.Snap:
    rs = await asyncio.gather(*(http.get(f"{u}/stats") for u in URLS.values()))
    return {name: r.json() for name, r in zip(URLS, rs)}


async def wait_until(pred, window_s: float, timeout_s: float) -> dict[str, Any] | None:
    """Poll every 0.5 s; return the window row once pred(row over the last window_s) holds."""
    snaps: list[tuple[float, probe.Snap]] = []
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            snaps.append((time.monotonic(), await snapshot()))
        except (httpx.HTTPError, ValueError) as e:
            log.info("probe failed: %s", e)
        while len(snaps) > 1 and snaps[-1][0] - snaps[1][0] >= window_s:
            snaps.pop(0)
        if len(snaps) > 1 and snaps[-1][0] - snaps[0][0] >= window_s:
            last = probe.row(snaps[0][1], snaps[-1][1])
            if pred(last):
                return last
        await asyncio.sleep(0.5)
    log.warning("wait_until timed out; last window: %s", last)
    return None


async def _orders_override(max_retries: int | None, ttl_s: float = 30) -> None:
    for u in (URLS["orders"], ORDERS_V2_URL):
        try:
            if max_retries is None:
                await http.delete(f"{u}/internal/retry_override", headers=TOKEN)
            else:
                await http.post(f"{u}/internal/retry_override", json={"max_retries": max_retries, "ttl_s": ttl_s},
                                headers=TOKEN)
        except httpx.HTTPError:
            if u == URLS["orders"]:
                raise


def _drained(r: dict[str, Any]) -> bool:
    return (r["pool_waiting"] or 0) == 0 and r["db_p99_ms"] is not None and r["db_p99_ms"] < ATTEMPT_TIMEOUT_MS / 2 \
        and (r["ok_ratio"] or 0) >= 0.98


async def do_reset() -> dict[str, Any]:
    t0 = time.monotonic()
    await _clear_physical()
    _state.update(world=World.none, params={}, started_at=None)
    # operator levers back to default (through the public control API, so its state agrees)
    for path in ("retry_override", "shed", "db/failover", "canary"):
        (await http.delete(f"{CONTROL_URL}/admin/{path}")).raise_for_status()
    (await http.delete(f"{URLS['loadgen']}/rate")).raise_for_status()
    # drain: without amplification offered load < capacity, so any queue (or a
    # self-sustaining storm) empties; then release retries and demand a stable baseline.
    for round_ in range(1, 4):
        remaining = RESET_TIMEOUT_S - (time.monotonic() - t0)
        await _orders_override(0, ttl_s=max(5.0, remaining))
        if await wait_until(_drained, 2.0, remaining) is None:
            break
        await _orders_override(None)
        remaining = RESET_TIMEOUT_S - (time.monotonic() - t0)
        row = await wait_until(lambda r: probe.is_healthy(r, ATTEMPT_TIMEOUT_MS), 5.0, min(15.0, remaining))
        if row is not None:
            log.info("reset complete in %.1fs (round %d)", time.monotonic() - t0, round_)
            return {"elapsed_s": round(time.monotonic() - t0, 1), "baseline": row}
    await _orders_override(None)
    raise HTTPException(status_code=503, detail="system did not return to a healthy baseline")


# ---- API -----------------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    for _ in range(60):  # make sure both DBs carry the configured base cost
        try:
            await set_db_extra(PRIMARY_DSN, 0)
            await set_db_extra(STANDBY_DSN, 0)
            break
        except (OSError, asyncpg.PostgresError) as e:
            log.info("db not ready: %s", e)
            await asyncio.sleep(1)
    yield


app = FastAPI(lifespan=lifespan)


async def _storm_end(duration_s: float) -> None:
    await asyncio.sleep(duration_s)
    await set_db_extra(PRIMARY_DSN, 0)
    log.info("storm trigger removed")


@app.post("/fault/storm")
async def storm(fault: StormFault | None = None):
    global _storm_task
    fault = fault or StormFault()
    async with _lock:
        await _clear_physical()
        await set_db_extra(PRIMARY_DSN, fault.delay_ms)
        _state["storm_until"] = time.monotonic() + fault.duration_s
        _storm_task = asyncio.create_task(_storm_end(fault.duration_s))
        _set_world(World.storm, fault.model_dump())
        log.info("storm: +%dms per query for %ds", fault.delay_ms, fault.duration_s)
        return _resp()


def degraded_extra_ms(capacity_qps: float) -> float:
    return max(0.0, DB_POOL_SIZE * 1000.0 / capacity_qps - DB_BASE_MS)


@app.post("/fault/degrade_db")
async def degrade_db(fault: DegradeDbFault | None = None):
    fault = fault or DegradeDbFault()
    if fault.capacity_qps <= 0:
        raise HTTPException(status_code=400, detail="capacity_qps must be > 0")
    async with _lock:
        await _clear_physical()
        extra = degraded_extra_ms(fault.capacity_qps)
        await set_db_extra(PRIMARY_DSN, extra)
        _set_world(World.degraded_db, fault.model_dump())
        log.info("degrade_db: capacity %.0f qps (+%.0fms per query)", fault.capacity_qps, extra)
        return _resp()


@app.post("/fault/cpu_starve")
async def cpu_starve(fault: CpuStarveFault | None = None):
    fault = fault or CpuStarveFault()
    if fault.cpus <= 0:
        raise HTTPException(status_code=400, detail="cpus must be > 0")
    async with _lock:
        await _clear_physical()
        try:
            await set_cpus(fault.service, fault.cpus)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=501, detail=f"docker socket unavailable: {e}")
        _set_world(World.cpu_starve, fault.model_dump())
        log.info("cpu_starve: %s limited to %.2f cpus", fault.service, fault.cpus)
        return _resp()


@app.post("/fault/reset")
async def reset():
    async with _lock:
        await do_reset()
        return _resp()


@app.get("/fault/state")
async def state():
    return _resp()


# ---- benchmark helpers (not part of C5) ----------------------------------------------------
@app.post("/debug/reset")
async def reset_verbose():
    """Same as /fault/reset but returns the baseline window it verified."""
    async with _lock:
        out = await do_reset()
        return {"state": fault_state().model_dump(mode="json"), **out}


@app.post("/debug/load")
async def set_load(body: dict[str, float]):
    r = await http.post(f"{URLS['loadgen']}/rate", json={"rps": body["rps"]})
    r.raise_for_status()
    return r.json()


@app.get("/debug/config")
async def config():
    return {"db_base_ms": DB_BASE_MS, "db_pool_size": DB_POOL_SIZE, "attempt_timeout_ms": ATTEMPT_TIMEOUT_MS,
            "primary_capacity_qps_nominal": DB_POOL_SIZE * 1000.0 / DB_BASE_MS}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
