"""C3 — Levers: every experiment, mitigation and code rollout goes through a LeverAdapter.

Every apply() carries a ttl_s dead-man switch: the target reverts the lever by itself when
it expires, so a crashed Faultline never leaves retries capped or traffic shed.
An experiment = apply(lever, params) -> hold hold_s -> undo -> watch the release.
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from .common import Model, utcnow


class LeverKind(str, Enum):
    traffic = "traffic"
    call_policy = "call_policy"
    dependency = "dependency"
    code = "code"


class LeverSpeed(str, Enum):
    config = "config"  # watch 15-30 s
    canary = "canary"  # watch minutes


class ActionStatus(str, Enum):
    active = "active"
    undone = "undone"
    expired = "expired"  # ttl hit, target reverted on its own
    failed = "failed"


class LeverSpec(Model):
    id: str
    kind: LeverKind
    description: str
    params_schema: dict[str, Any]  # JSON Schema for params
    speed: LeverSpeed
    reversible: bool = True
    default_watch_s: int
    max_ttl_s: int


class Experiment(Model):
    id: str  # e.g. "retry_cap_0_20s"
    lever_id: str
    params: dict[str, Any]
    hold_s: int
    blast_radius_pct: float  # % of otherwise-successful user requests affected


class UndoSpec(Model):
    """Enough to undo an action from a fresh process (serializable)."""

    lever_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ActionHandle(Model):
    action_id: str
    lever_id: str
    params: dict[str, Any]
    applied_at: datetime = Field(default_factory=utcnow)
    ttl_s: int
    status: ActionStatus = ActionStatus.active
    undo: UndoSpec

    @property
    def expires_at(self) -> datetime:
        return self.applied_at + timedelta(seconds=self.ttl_s)


class LeverError(Exception):
    """Bad lever id/params, or the target refused the action."""


@runtime_checkable
class LeverAdapter(Protocol):
    def catalog(self) -> list[LeverSpec]: ...

    def estimate_blast_radius(self, lever_id: str, params: dict[str, Any]) -> float: ...

    def apply(self, lever_id: str, params: dict[str, Any], ttl_s: int) -> ActionHandle: ...

    def undo(self, handle: ActionHandle) -> ActionHandle: ...

    def status(self, handle: ActionHandle) -> ActionStatus: ...


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# Standard catalog for the sandbox. Real adapters should return these ids.
CATALOG: list[LeverSpec] = [
    LeverSpec(
        id="retry_cap",
        kind=LeverKind.call_policy,
        description="Cap the retries Orders makes to Payments (runtime override).",
        params_schema=_obj({"max_retries": {"type": "integer", "minimum": 0, "maximum": 3}}, ["max_retries"]),
        speed=LeverSpeed.config,
        default_watch_s=20,
        max_ttl_s=300,
    ),
    LeverSpec(
        id="shed",
        kind=LeverKind.traffic,
        description="Reject a fraction of incoming checkout traffic at the gateway (Envoy).",
        params_schema=_obj({"fraction": {"type": "number", "minimum": 0, "maximum": 1}}, ["fraction"]),
        speed=LeverSpeed.config,
        default_watch_s=20,
        max_ttl_s=300,
    ),
    LeverSpec(
        id="db_failover",
        kind=LeverKind.dependency,
        description="Move Payments to the standby DB path (pauses the batch job in the sandbox).",
        params_schema=_obj({}, []),
        speed=LeverSpeed.config,
        default_watch_s=30,
        max_ttl_s=900,
    ),
    LeverSpec(
        id="canary_weight",
        kind=LeverKind.code,
        description="Send a fraction of Orders traffic to orders-v2 (Envoy weighted cluster).",
        params_schema=_obj({"v2_weight": {"type": "number", "minimum": 0, "maximum": 1}}, ["v2_weight"]),
        speed=LeverSpeed.canary,
        default_watch_s=120,
        max_ttl_s=1800,
    ),
]


def standard_blast_radius(lever_id: str, params: dict[str, Any]) -> float:
    """Default blast-radius estimates (% of successful requests affected) for CATALOG levers."""
    if lever_id == "retry_cap":
        return 0.0  # only removes retries of already-failing calls
    if lever_id == "shed":
        return 100.0 * float(params["fraction"])
    if lever_id == "db_failover":
        return 1.0
    if lever_id == "canary_weight":
        return 100.0 * float(params["v2_weight"])
    raise LeverError(f"unknown lever {lever_id!r}")
