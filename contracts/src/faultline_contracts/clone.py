"""C6 — Clone lab: disposable, clean copies of the target where investigators run aggressive,
counterfactual experiments before anything touches production.

Three environments, never mixed:
  production   hidden cause; Faultline may only use C3 levers (control service :9901)
  clean clone  starts healthy; built ONLY from observable state (versions, config, retry policy,
               workload rate); C6 lab actions allowed. Never inherits fault-controller state.
  benchmark    C5 (:9900); invisible to Faultline and to investigators.

A clone is a full replica of the target (own Compose project and network). It exposes the same
surfaces production does — `/stats` for telemetry and a C3 control service for levers — so the
production telemetry and lever adapters work unchanged when pointed at `CloneInfo.endpoints`.
On top of that, and only in clones, the lab API offers LAB_CATALOG: physical primitives that
change what the clone's world *is* (DB latency/capacity, CPU, retry policy, kill/restart).

HTTP API served by the clone manager (default http://localhost:9910):
  GET    /lab/catalog                          -> list[LabActionSpec]
  GET    /clones                               -> list[CloneInfo]
  POST   /clones                CloneSpec      -> CloneInfo   (blocks until ready; 409 if at capacity)
  GET    /clones/{id}                          -> CloneInfo
  POST   /clones/{id}/reset                    -> CloneInfo   (blocks until a healthy 5 s window)
  DELETE /clones/{id}                          -> CloneInfo   (status destroyed; idempotent)
  POST   /clones/{id}/workload  WorkloadSpec   -> CloneInfo
  POST   /clones/{id}/actions   LabActionRequest -> LabActionHandle
  DELETE /clones/{id}/actions/{action_id}      -> LabActionHandle (status undone; idempotent)
  GET    /clones/{id}/actions                  -> list[LabActionHandle]
  GET    /healthz
Errors: 400 bad params / ttl out of range, 404 unknown clone or action, 409 refused (capacity,
clone not ready), 503 the clone could not reach a healthy baseline. Every lab action carries a
ttl_s and the clone reverts it on its own when it expires.
"""

# CloneLab.list() shadows the builtin inside the class body; deferred annotations keep
# `-> list[LabActionHandle]` importable on Python < 3.14.
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import Field

from .common import Model, utcnow
from .levers import ActionStatus, _obj

DEFAULT_LAB_URL = "http://localhost:9910"
MAX_CLONES = 3  # production + 2 investigation clones is the budget; 3 only after profiling


class CloneStatus(str, Enum):
    creating = "creating"
    ready = "ready"  # healthy baseline verified, accepting actions
    resetting = "resetting"
    destroyed = "destroyed"
    failed = "failed"


class RetryPolicy(Model):
    """Orders' call policy towards Payments — observable production config."""

    max_retries: int = Field(3, ge=0, le=5)
    timeout_ms: int = Field(500, ge=50, le=10_000)


class WorkloadSpec(Model):
    """Open-loop request rate to replay into a clone (reconstructed from observed production load)."""

    rps: float = Field(80.0, gt=0, le=1000)


class CloneSpec(Model):
    """What a clone inherits: only what Faultline may legitimately know about production."""

    name: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")  # display label, e.g. the hypothesis id
    versions: dict[str, str] = Field(default_factory=lambda: {"orders": "v1", "payments": "v1"})
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    workload: WorkloadSpec = Field(default_factory=WorkloadSpec)
    patch_ref: str | None = None  # git ref / checkout path built into the clone's orders-v2 slot


class CloneEndpoints(Model):
    """Same surfaces as production, so the C1 and C3 adapters work unchanged against a clone."""

    gateway_url: str  # Envoy front door (demo traffic)
    control_url: str  # C3 control service for this clone (retry_cap, shed, db_failover, canary_weight)
    stats_urls: dict[str, str]  # service -> GET /stats base url (orders, orders-v2, payments, loadgen)


class LabActionSpec(Model):
    id: str
    description: str
    params_schema: dict[str, Any]  # JSON Schema for params
    reversible: bool = True  # False = one-shot (restart); ttl_s is accepted but has no effect
    max_ttl_s: int


class LabActionRequest(Model):
    action: str  # LabActionSpec.id
    params: dict[str, Any] = Field(default_factory=dict)
    ttl_s: int = Field(ge=1)


class LabActionHandle(Model):
    action_id: str
    clone_id: str
    action: str
    params: dict[str, Any]
    applied_at: datetime = Field(default_factory=utcnow)
    ttl_s: int
    status: ActionStatus = ActionStatus.active

    @property
    def expires_at(self) -> datetime:
        return self.applied_at + timedelta(seconds=self.ttl_s)


class CloneInfo(Model):
    clone_id: str
    status: CloneStatus
    spec: CloneSpec
    created_at: datetime
    endpoints: CloneEndpoints | None = None  # None until ready / after destroy
    active_actions: list[LabActionHandle] = Field(default_factory=list)
    detail: str | None = None  # e.g. why it failed


class LabError(Exception):
    """Bad action id/params, unknown clone, or the lab refused (capacity, clone not ready)."""


# Lab primitives (clone-only). Wording avoids hidden-world vocabulary on purpose: investigators read this.
_SERVICE = {"type": "string", "enum": ["orders", "orders-v2", "payments"]}

LAB_CATALOG: list[LabActionSpec] = [
    LabActionSpec(
        id="retry_policy",
        description="Set Orders' retry count and per-attempt timeout towards Payments.",
        params_schema=_obj({"max_retries": {"type": "integer", "minimum": 0, "maximum": 5},
                            "timeout_ms": {"type": "integer", "minimum": 50, "maximum": 10000}}, []),
        max_ttl_s=1800,
    ),
    LabActionSpec(
        id="db_latency",
        description="Add extra_ms to every query on the primary DB for ttl_s seconds, then remove it "
                    "(a transient slowdown of the database).",
        params_schema=_obj({"extra_ms": {"type": "integer", "minimum": 0, "maximum": 5000}}, ["extra_ms"]),
        max_ttl_s=600,
    ),
    LabActionSpec(
        id="db_capacity",
        description="Limit the primary DB to capacity_qps queries/s for the app (what a runaway batch job "
                    "does); undo = the job is paused. The standby DB is untouched.",
        params_schema=_obj({"capacity_qps": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000}},
                           ["capacity_qps"]),
        max_ttl_s=1800,
    ),
    LabActionSpec(
        id="cpu_limit",
        description="Limit a service container to `cpus` CPUs (a noisy neighbour).",
        params_schema=_obj({"service": _SERVICE, "cpus": {"type": "number", "minimum": 0.05, "maximum": 8}},
                           ["service", "cpus"]),
        max_ttl_s=1800,
    ),
    LabActionSpec(
        id="service_kill",
        description="Stop a service container for ttl_s seconds, then start it again.",
        params_schema=_obj({"service": _SERVICE}, ["service"]),
        max_ttl_s=300,
    ),
    LabActionSpec(
        id="service_restart",
        description="Restart a service container once (drops in-flight requests and state).",
        params_schema=_obj({"service": _SERVICE}, ["service"]),
        reversible=False,
        max_ttl_s=1,
    ),
]

LAB_ACTION_IDS = {a.id for a in LAB_CATALOG}


@runtime_checkable
class CloneLab(Protocol):
    def catalog(self) -> list[LabActionSpec]: ...

    def list(self) -> list[CloneInfo]: ...

    def create(self, spec: CloneSpec) -> CloneInfo: ...

    def get(self, clone_id: str) -> CloneInfo: ...

    def reset(self, clone_id: str) -> CloneInfo: ...

    def destroy(self, clone_id: str) -> CloneInfo: ...

    def set_workload(self, clone_id: str, workload: WorkloadSpec) -> CloneInfo: ...

    def apply(self, clone_id: str, action: str, params: dict[str, Any], ttl_s: int) -> LabActionHandle: ...

    def undo(self, handle: LabActionHandle) -> LabActionHandle: ...

    def actions(self, clone_id: str) -> list[LabActionHandle]: ...


class HttpCloneLab:
    """Client for the clone manager. create()/reset() block until the clone is healthy, so the
    timeout is long by default."""

    def __init__(self, base_url: str = DEFAULT_LAB_URL, timeout_s: float = 180.0):
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s)

    def _call(self, method: str, path: str, body: Model | None = None) -> Any:
        r = self._http.request(method, path, json=body.model_dump(mode="json") if body else None)
        if 400 <= r.status_code < 500:
            raise LabError(f"{method} {path} -> {r.status_code}: {r.json().get('detail', r.text)}")
        r.raise_for_status()
        return r.json()

    def catalog(self) -> list[LabActionSpec]:
        return [LabActionSpec.model_validate(a) for a in self._call("GET", "/lab/catalog")]

    def list(self) -> list[CloneInfo]:
        return [CloneInfo.model_validate(c) for c in self._call("GET", "/clones")]

    def create(self, spec: CloneSpec) -> CloneInfo:
        return CloneInfo.model_validate(self._call("POST", "/clones", spec))

    def get(self, clone_id: str) -> CloneInfo:
        return CloneInfo.model_validate(self._call("GET", f"/clones/{clone_id}"))

    def reset(self, clone_id: str) -> CloneInfo:
        return CloneInfo.model_validate(self._call("POST", f"/clones/{clone_id}/reset"))

    def destroy(self, clone_id: str) -> CloneInfo:
        return CloneInfo.model_validate(self._call("DELETE", f"/clones/{clone_id}"))

    def set_workload(self, clone_id: str, workload: WorkloadSpec) -> CloneInfo:
        return CloneInfo.model_validate(self._call("POST", f"/clones/{clone_id}/workload", workload))

    def apply(self, clone_id: str, action: str, params: dict[str, Any], ttl_s: int) -> LabActionHandle:
        if action not in LAB_ACTION_IDS:
            raise LabError(f"unknown lab action {action!r}")
        req = LabActionRequest(action=action, params=params, ttl_s=ttl_s)
        return LabActionHandle.model_validate(self._call("POST", f"/clones/{clone_id}/actions", req))

    def undo(self, handle: LabActionHandle) -> LabActionHandle:
        return LabActionHandle.model_validate(
            self._call("DELETE", f"/clones/{handle.clone_id}/actions/{handle.action_id}"))

    def actions(self, clone_id: str) -> list[LabActionHandle]:
        return [LabActionHandle.model_validate(a) for a in self._call("GET", f"/clones/{clone_id}/actions")]
