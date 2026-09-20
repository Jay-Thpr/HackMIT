"""Sandbox control service (:9901): the legitimate operator levers Faultline may pull.

  POST/DELETE /admin/retry_override   -> Orders /internal/retry_override (Orders also expires it itself)
  POST/DELETE /admin/shed             -> Envoy runtime fault.http.abort.abort_percent
  POST/DELETE /admin/db/failover      -> Payments /internal/db_target (Payments also expires it itself)
  POST/DELETE /admin/canary           -> Envoy runtime routing.traffic_shift.orders (per 10000)
  GET /admin/levers, GET /healthz

Dead-man switch: every POST needs ttl_s. Orders and Payments hold the expiry themselves, so
those levers revert even if this service dies. Envoy runtime has no ttl, so this service
reverts it on expiry, re-asserts the desired value every second (survives an Envoy restart),
and zeroes it on startup (survives a restart of this service).

This service knows nothing about the hidden fault controller.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from faultline_contracts.levers import CATALOG

log = logging.getLogger("control")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

ORDERS_URLS = [u for u in os.environ.get("ORDERS_URLS", "http://orders:8000").split(",") if u]
ORDERS_V2_URL = os.environ.get("ORDERS_V2_URL", "http://orders-v2:8000")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://payments:8000")
ENVOY_ADMIN = os.environ.get("ENVOY_ADMIN_URL", "http://envoy:9902")
TOKEN = {"X-Sandbox-Token": os.environ.get("SANDBOX_TOKEN", "sandbox-internal")}

SHED_KEY = "fault.http.abort.abort_percent"  # Envoy HTTP fault filter used as a load shedder
CANARY_KEY = "routing.traffic_shift.orders"  # numerator over 10000 for orders-v2

MAX_TTL = {s.id: s.max_ttl_s for s in CATALOG}
PATHS = {"retry_cap": "retry_override", "shed": "shed", "db_failover": "db/failover", "canary_weight": "canary"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime | None) -> str | None:
    return t.isoformat().replace("+00:00", "Z") if t else None


class Lever:
    def __init__(self, lever_id: str) -> None:
        self.lever_id = lever_id
        self.params: dict[str, Any] = {}
        self.applied_at: datetime | None = None
        self.expires_at: datetime | None = None
        self.active = False

    def view(self) -> dict[str, Any]:
        return {"lever_id": self.lever_id, "params": self.params, "applied_at": iso(self.applied_at),
                "expires_at": iso(self.expires_at), "active": self.active}


levers = {lid: Lever(lid) for lid in PATHS}
_lock = asyncio.Lock()
http = httpx.AsyncClient(timeout=3.0)


def canary_runtime_value(weight: float) -> str:
    return json.dumps({"numerator": round(10000 * weight), "denominator": "TEN_THOUSAND"}, separators=(",", ":"))


async def envoy_runtime(key: str, value: int | str) -> None:
    r = await http.post(f"{ENVOY_ADMIN}/runtime_modify", params={key: str(value)})
    r.raise_for_status()


async def push(lever_id: str, params: dict[str, Any], ttl_s: float) -> None:
    """Apply a lever to its target. Raises httpx errors if the target is unreachable."""
    if lever_id == "retry_cap":
        body = {"max_retries": params["max_retries"], "ttl_s": ttl_s}
        r = await http.post(f"{ORDERS_URLS[0]}/internal/retry_override", json=body, headers=TOKEN)
        r.raise_for_status()
        for extra in [*ORDERS_URLS[1:], ORDERS_V2_URL]:  # best effort: other Orders builds
            try:
                await http.post(f"{extra}/internal/retry_override", json=body, headers=TOKEN, timeout=0.5)
            except httpx.HTTPError:
                pass
    elif lever_id == "db_failover":
        r = await http.post(f"{PAYMENTS_URL}/internal/db_target", json={"target": "standby", "ttl_s": ttl_s},
                            headers=TOKEN)
        r.raise_for_status()
    elif lever_id == "shed":
        await envoy_runtime(SHED_KEY, round(100 * params["fraction"]))
    elif lever_id == "canary_weight":
        await envoy_runtime(CANARY_KEY, canary_runtime_value(params["v2_weight"]))


async def revert(lever_id: str) -> None:
    """Return a lever's target to default. The primary target must confirm; secondary Orders builds are best effort."""
    if lever_id == "retry_cap":
        r = await http.delete(f"{ORDERS_URLS[0]}/internal/retry_override", headers=TOKEN)
        r.raise_for_status()
        g = await http.get(f"{ORDERS_URLS[0]}/internal/retry_override", headers=TOKEN)
        g.raise_for_status()
        if g.json().get("override") is not None or g.json().get("timeout_override_ms") is not None:
            raise httpx.HTTPError(f"orders still reports retry override {g.json()}")
        for u in [*ORDERS_URLS[1:], ORDERS_V2_URL]:  # best effort: other Orders builds
            try:
                (await http.delete(f"{u}/internal/retry_override", headers=TOKEN, timeout=1.0)).raise_for_status()
            except httpx.HTTPError:
                pass
    elif lever_id == "db_failover":
        await http.delete(f"{PAYMENTS_URL}/internal/db_target", headers=TOKEN)
    elif lever_id == "shed":
        await envoy_runtime(SHED_KEY, 0)
    elif lever_id == "canary_weight":
        await envoy_runtime(CANARY_KEY, canary_runtime_value(0))


async def _reconcile_loop() -> None:
    """Expire levers and keep Envoy runtime equal to the desired state."""
    while True:
        try:
            async with _lock:
                t = now()
                for lv in levers.values():
                    if lv.active and lv.expires_at and t >= lv.expires_at:
                        log.info("lever %s ttl expired, reverting", lv.lever_id)
                        lv.active = False
                        try:
                            await revert(lv.lever_id)
                        except httpx.HTTPError as e:
                            log.warning("revert %s failed: %s (will retry)", lv.lever_id, e)
                            lv.active, lv.expires_at = True, t  # retry next tick
                shed, canary = levers["shed"], levers["canary_weight"]
                await envoy_runtime(SHED_KEY, round(100 * shed.params["fraction"]) if shed.active else 0)
                await envoy_runtime(CANARY_KEY, canary_runtime_value(canary.params["v2_weight"]) if canary.active else canary_runtime_value(0))
        except Exception as e:  # noqa: BLE001 - never let the dead-man switch die
            log.warning("reconcile error: %s", e)
        await asyncio.sleep(1.0)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_reconcile_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


def _bad(msg: str) -> HTTPException:
    return HTTPException(status_code=400, detail=msg)


def _validate(lever_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    ttl = body.get("ttl_s")
    if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not (1 <= ttl <= MAX_TTL[lever_id]):
        raise _bad(f"ttl_s must be a number in [1, {MAX_TTL[lever_id]}]")
    extra = set(body) - {"ttl_s", "max_retries", "fraction", "v2_weight"}
    if extra:
        raise _bad(f"unknown fields: {sorted(extra)}")
    if lever_id == "retry_cap":
        v = body.get("max_retries")
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 3:
            raise _bad("max_retries must be an integer in [0, 3]")
        return {"max_retries": v}, ttl
    if lever_id == "shed":
        v = body.get("fraction")
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 1:
            raise _bad("fraction must be a number in [0, 1]")
        return {"fraction": float(v)}, ttl
    if lever_id == "canary_weight":
        v = body.get("v2_weight")
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 1:
            raise _bad("v2_weight must be a number in [0, 1]")
        return {"v2_weight": float(v)}, ttl
    if set(body) - {"ttl_s"}:
        raise _bad("db_failover takes no params")
    return {}, ttl


async def _apply(lever_id: str, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        raise _bad("body must be JSON")
    if not isinstance(body, dict):
        raise _bad("body must be a JSON object")
    params, ttl = _validate(lever_id, body)
    if lever_id == "canary_weight" and params["v2_weight"] > 0:
        try:
            (await http.get(f"{ORDERS_V2_URL}/healthz", timeout=1.0)).raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(status_code=409, detail="orders-v2 is not running; build and start it first")
    async with _lock:
        try:
            await push(lever_id, params, ttl)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"target unreachable: {e}")
        lv = levers[lever_id]
        lv.params, lv.applied_at, lv.active = params, now(), True
        lv.expires_at = lv.applied_at + timedelta(seconds=ttl)
        log.info("lever %s applied %s for %ss", lever_id, params, ttl)
        return JSONResponse(lv.view())


async def _undo(lever_id: str) -> JSONResponse:
    async with _lock:
        try:
            await revert(lever_id)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"target unreachable: {e}")
        lv = levers[lever_id]
        if lv.active:
            log.info("lever %s undone", lever_id)
        lv.active = False
        return JSONResponse(lv.view())


def _routes(lever_id: str, path: str) -> None:
    async def post(request: Request):
        return await _apply(lever_id, request)

    async def delete():
        return await _undo(lever_id)

    app.add_api_route(f"/admin/{path}", post, methods=["POST"], name=f"apply_{lever_id}")
    app.add_api_route(f"/admin/{path}", delete, methods=["DELETE"], name=f"undo_{lever_id}")


for _lid, _path in PATHS.items():
    _routes(_lid, _path)


@app.get("/admin/levers")
async def get_levers():
    t = now()
    out = {}
    for lv in levers.values():
        active = lv.active and not (lv.expires_at and t >= lv.expires_at)
        out[lv.lever_id] = {"active": active, "params": lv.params, "expires_at": iso(lv.expires_at)}
    return out


@app.get("/healthz")
async def healthz():
    return {"ok": True}
