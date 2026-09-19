from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from faultline_contracts import (
    CATALOG,
    ActionHandle,
    ActionStatus,
    Experiment,
    Fingerprint,
    LeverError,
    LeverSpec,
    TriageResult,
    UndoSpec,
    Verdict,
    standard_blast_radius,
    utcnow,
)


class FixtureClock:
    def __init__(self, now: datetime, limit: datetime | None = None):
        self.now = now
        self.limit = limit

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        if self.limit is not None and self.now > self.limit:
            self.now = self.limit


class FixtureBrain:
    def __init__(self, triage: TriageResult, experiment: Experiment, verdict: Verdict):
        self._triage = triage
        self._experiment = experiment
        self._verdict = verdict
        self._incident_id = triage.incident_id

    def triage(self, incident_id: str, fingerprint: Fingerprint) -> TriageResult:
        del fingerprint
        self._incident_id = incident_id
        return self._triage.model_copy(update={"incident_id": incident_id})

    def plan(self, triage, catalog, blast_radius) -> Experiment:
        del triage, catalog, blast_radius
        return self._experiment

    def judge(self, triage, experiment, baseline, during, after_release) -> Verdict:
        del triage, experiment, baseline, during, after_release
        return self._verdict.model_copy(update={"incident_id": self._incident_id})


def validate_params(spec: LeverSpec, params: dict[str, Any], ttl_s: int) -> None:
    if ttl_s <= 0 or ttl_s > spec.max_ttl_s:
        raise LeverError(f"invalid ttl_s {ttl_s} for {spec.id}")
    schema = spec.params_schema
    properties = schema.get("properties", {})
    missing = set(schema.get("required", [])) - params.keys()
    unexpected = params.keys() - properties.keys()
    if missing:
        raise LeverError(f"{spec.id}: missing params {sorted(missing)}")
    if unexpected:
        raise LeverError(f"{spec.id}: unexpected params {sorted(unexpected)}")
    for key, value in params.items():
        rule = properties[key]
        expected = rule.get("type")
        valid = (
            expected == "integer"
            and isinstance(value, int)
            and not isinstance(value, bool)
            or expected == "number"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
        if not valid:
            raise LeverError(f"{spec.id}.{key}: expected {expected}")
        if "minimum" in rule and value < rule["minimum"]:
            raise LeverError(f"{spec.id}.{key}: below minimum")
        if "maximum" in rule and value > rule["maximum"]:
            raise LeverError(f"{spec.id}.{key}: above maximum")


class FixtureLeverAdapter:
    """Public-C3-only adapter for local CLI runs."""

    def __init__(self, clock: Callable[[], datetime] = utcnow):
        self._clock = clock
        self._specs = {spec.id: spec for spec in CATALOG}
        self._actions: dict[str, ActionHandle] = {}

    def catalog(self) -> list[LeverSpec]:
        return list(self._specs.values())

    def estimate_blast_radius(self, lever_id: str, params: dict[str, Any]) -> float:
        spec = self._specs.get(lever_id)
        if spec is None:
            raise LeverError(f"unknown lever {lever_id!r}")
        validate_params(spec, params, ttl_s=1)
        return standard_blast_radius(lever_id, params)

    def apply(self, lever_id: str, params: dict[str, Any], ttl_s: int) -> ActionHandle:
        spec = self._specs.get(lever_id)
        if spec is None:
            raise LeverError(f"unknown lever {lever_id!r}")
        validate_params(spec, params, ttl_s)
        handle = ActionHandle(
            action_id=uuid4().hex,
            lever_id=lever_id,
            params=dict(params),
            applied_at=self._clock(),
            ttl_s=ttl_s,
            undo=UndoSpec(lever_id=lever_id, payload={"params": dict(params)}),
        )
        self._actions[handle.action_id] = handle
        return handle

    def undo(self, handle: ActionHandle) -> ActionHandle:
        current = self._refresh(handle.action_id)
        if current.status == ActionStatus.active:
            current = current.model_copy(update={"status": ActionStatus.undone})
            self._actions[current.action_id] = current
        return current

    def status(self, handle: ActionHandle) -> ActionStatus:
        return self._refresh(handle.action_id).status

    def _require(self, action_id: str) -> ActionHandle:
        try:
            return self._actions[action_id]
        except KeyError as exc:
            raise LeverError(f"unknown action {action_id!r}") from exc

    def _refresh(self, action_id: str) -> ActionHandle:
        current = self._require(action_id)
        if current.status == ActionStatus.active and self._clock() >= current.expires_at:
            current = current.model_copy(update={"status": ActionStatus.expired})
            self._actions[current.action_id] = current
        return current


class FixtureTelemetrySource:
    """C1-compatible replay source without importing hidden benchmark fakes."""

    def __init__(self, fingerprints: list[Fingerprint]):
        if not fingerprints:
            raise ValueError("at least one fingerprint is required")
        self._fingerprints = sorted(fingerprints, key=lambda item: item.window_start)

    def window(self, start: datetime, end: datetime) -> Fingerprint:
        matches = [
            item
            for item in self._fingerprints
            if item.window_start < end and item.window_end > start
        ]
        if not matches:
            raise ValueError(f"no recorded window overlaps [{start}, {end})")
        return max(matches, key=lambda item: item.window_end)

    def series(self, start: datetime, end: datetime, step_s: int = 5) -> list[Fingerprint]:
        del step_s
        return [
            item
            for item in self._fingerprints
            if item.window_start < end and item.window_end > start
        ]

    def first_breach(self) -> Fingerprint:
        for item in self._fingerprints:
            if any(slo.breached for slo in item.slos):
                return item
        raise ValueError("fixture contains no breached SLO window")

    @property
    def last_window_end(self) -> datetime:
        return self._fingerprints[-1].window_end
