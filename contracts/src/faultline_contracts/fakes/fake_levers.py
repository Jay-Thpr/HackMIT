"""FakeLeverAdapter: in-memory LeverAdapter that only records what was applied / undone.

Use it for orchestrator unit tests. Pass `clock` (a zero-arg callable returning an aware
datetime) to control TTL expiry, e.g. `ManualClock`.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..common import utcnow
from ..levers import CATALOG, ActionHandle, ActionStatus, LeverError, LeverSpec, UndoSpec, standard_blast_radius

_SPECS = {s.id: s for s in CATALOG}


def validate_lever_call(lever_id: str, params: dict[str, Any], ttl_s: int, specs: dict[str, LeverSpec] = _SPECS) -> LeverSpec:
    """Minimal JSON-schema check of params against the catalog spec. Raises LeverError."""
    spec = specs.get(lever_id)
    if spec is None:
        raise LeverError(f"unknown lever {lever_id!r}")
    if not isinstance(ttl_s, int) or ttl_s <= 0:
        raise LeverError(f"ttl_s must be a positive int, got {ttl_s!r}")
    if ttl_s > spec.max_ttl_s:
        raise LeverError(f"ttl_s {ttl_s} exceeds max_ttl_s {spec.max_ttl_s} for {lever_id}")
    schema = spec.params_schema
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in params:
            raise LeverError(f"{lever_id}: missing param {req!r}")
    for key, value in params.items():
        if key not in props:
            raise LeverError(f"{lever_id}: unexpected param {key!r}")
        p = props[key]
        if isinstance(value, bool):
            raise LeverError(f"{lever_id}.{key}: expected {p.get('type')}, got bool")
        if p.get("type") == "integer" and not isinstance(value, int):
            raise LeverError(f"{lever_id}.{key}: expected integer, got {value!r}")
        if p.get("type") == "number" and not isinstance(value, (int, float)):
            raise LeverError(f"{lever_id}.{key}: expected number, got {value!r}")
        if "minimum" in p and value < p["minimum"]:
            raise LeverError(f"{lever_id}.{key}: {value} < minimum {p['minimum']}")
        if "maximum" in p and value > p["maximum"]:
            raise LeverError(f"{lever_id}.{key}: {value} > maximum {p['maximum']}")
    return spec


class ManualClock:
    """Controllable clock: `clock()` returns now, `clock.advance(s)` moves it forward."""

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


class FakeLeverAdapter:
    """Records actions; no effect on any system. `applied` / `undone` / `expired` are handle lists."""

    def __init__(self, clock: Callable[[], datetime] | None = None, catalog: list[LeverSpec] | None = None):
        self.clock = clock or utcnow
        self._catalog = list(catalog or CATALOG)
        self._specs = {s.id: s for s in self._catalog}
        self.actions: dict[str, ActionHandle] = {}
        self.applied: list[ActionHandle] = []
        self.undone: list[ActionHandle] = []
        self.expired: list[ActionHandle] = []

    def catalog(self) -> list[LeverSpec]:
        return list(self._catalog)

    def estimate_blast_radius(self, lever_id: str, params: dict[str, Any]) -> float:
        return standard_blast_radius(lever_id, params)

    def apply(self, lever_id: str, params: dict[str, Any], ttl_s: int) -> ActionHandle:
        validate_lever_call(lever_id, params, ttl_s, self._specs)
        h = ActionHandle(
            action_id=uuid4().hex,
            lever_id=lever_id,
            params=dict(params),
            applied_at=self.clock(),
            ttl_s=ttl_s,
            undo=UndoSpec(lever_id=lever_id, payload={"params": dict(params)}),
        )
        self.actions[h.action_id] = h
        self.applied.append(h)
        return h

    def _refresh(self, action_id: str) -> ActionHandle:
        h = self.actions.get(action_id)
        if h is None:
            raise LeverError(f"unknown action {action_id!r}")
        if h.status == ActionStatus.active and self.clock() >= h.expires_at:
            h = h.model_copy(update={"status": ActionStatus.expired})
            self.actions[action_id] = h
            self.expired.append(h)
        return h

    def undo(self, handle: ActionHandle) -> ActionHandle:
        h = self._refresh(handle.action_id)
        if h.status != ActionStatus.active:
            return h  # already reverted (undone or expired): idempotent
        h = h.model_copy(update={"status": ActionStatus.undone})
        self.actions[h.action_id] = h
        self.undone.append(h)
        return h

    def status(self, handle: ActionHandle) -> ActionStatus:
        return self._refresh(handle.action_id).status

    def active(self) -> list[ActionHandle]:
        return [h for aid in list(self.actions) if (h := self._refresh(aid)).status == ActionStatus.active]
