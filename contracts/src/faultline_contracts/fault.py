"""C5 — Fault controller. HIDDEN FROM FAULTLINE: only the sandbox and bench/ may import this.
A test enforces that nothing under faultline/ imports it.

HTTP API served by the sandbox fault controller (default http://localhost:9900):
  POST /fault/storm        StormFault
  POST /fault/degrade_db   DegradeDbFault
  POST /fault/cpu_starve   CpuStarveFault
  POST /fault/reset
  GET  /fault/state        -> FaultState
"""

from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import Field

from .common import Model

DEFAULT_FAULT_URL = "http://localhost:9900"


class World(str, Enum):
    none = "none"
    storm = "storm"  # World A: transient DB delay triggers a self-sustaining retry storm
    degraded_db = "degraded_db"  # World B: batch job cuts DB capacity
    cpu_starve = "cpu_starve"  # none-of-the-above: Payments CPU starved


class StormFault(Model):
    delay_ms: int = 800
    duration_s: int = 20


class DegradeDbFault(Model):
    capacity_qps: float = 40.0


class CpuStarveFault(Model):
    service: str = "payments"
    cpus: float = 0.1


class FaultState(Model):
    world: World
    active: bool  # trigger currently being applied (storm: False after duration_s ends)
    params: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None


@runtime_checkable
class FaultController(Protocol):
    def storm(self, fault: StormFault) -> FaultState: ...

    def degrade_db(self, fault: DegradeDbFault) -> FaultState: ...

    def cpu_starve(self, fault: CpuStarveFault) -> FaultState: ...

    def reset(self) -> FaultState: ...

    def state(self) -> FaultState: ...


class HttpFaultController:
    """Client for the sandbox fault controller."""

    def __init__(self, base_url: str = DEFAULT_FAULT_URL, timeout_s: float = 5.0):
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s)

    def _post(self, path: str, body: Model | None = None) -> FaultState:
        r = self._http.post(path, json=body.model_dump() if body else None)
        r.raise_for_status()
        return FaultState.model_validate(r.json())

    def storm(self, fault: StormFault) -> FaultState:
        return self._post("/fault/storm", fault)

    def degrade_db(self, fault: DegradeDbFault) -> FaultState:
        return self._post("/fault/degrade_db", fault)

    def cpu_starve(self, fault: CpuStarveFault) -> FaultState:
        return self._post("/fault/cpu_starve", fault)

    def reset(self) -> FaultState:
        return self._post("/fault/reset")

    def state(self) -> FaultState:
        r = self._http.get("/fault/state")
        r.raise_for_status()
        return FaultState.model_validate(r.json())
