"""C1 and C3 adapters for the advanced distributed stack.

The stack speaks one HTTP surface, the in-cluster control service (Bearer token, reached
through a host port-forward): `GET /snapshot` for observable telemetry, `GET /catalog` plus
`POST/GET/DELETE /actions` for the five reversible levers. Faultline sees nothing else --
the injected cause lives in `integration/advanced_faults.py`, on the host, out of reach.

Telemetry conversion is Owner 2's `fingerprint_from_distributed`; this module only polls,
slices windows, and decides which SLOs to assert. Backlog is the user-facing symptom here:
an order accepted but not yet fulfilled is a customer waiting, so the SLO is per tenant on
`oldest_pending_ms`. Healthy runs sit near zero with occasional sub-second spikes, and a
starved consumer climbs into tens of seconds, so the threshold sits well clear of both.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from faultline_contracts import WINDOW_S, ActionHandle, ActionStatus, Fingerprint, LeverSpec
from faultline_contracts.levers import LeverError
from faultline_telemetry.distributed import fingerprint_from_distributed

from .fixture import validate_params
from .live_telemetry import FingerprintWriter, TelemetryUnavailable

log = logging.getLogger(__name__)

# An order accepted but not yet fulfilled is a customer waiting. Healthy windows sit near zero
# with sub-second spikes; a starved consumer reaches tens of seconds within a minute.
BACKLOG_SLO_MS = 5000.0
# `oldest_pending_ms` only exists while something is pending, so a healthy window would carry no
# SLO at all and the UI could not tell "measured healthy" from "never measured". `outstanding` is
# always reported, so it carries the assertion and the age metric refines it when present.
BACKLOG_SLO_ORDERS = 15.0
TENANT_PREFIX = "tenant_"
# Parameters that scope a lever to part of the system, most specific first.
SCOPE_PARAMS = ("tenant", "partition", "shard", "worker")


def _request(method: str, url: str, token: str, data: dict | None = None, timeout: float = 10.0) -> tuple[int, Any]:
    payload = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(
        url, data=payload, method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            return error.code, json.loads(body) if body else {}
        except json.JSONDecodeError:
            return error.code, {"detail": body[:200].decode(errors="replace")}


def backlog_thresholds(snapshot: dict, limit_ms: float = BACKLOG_SLO_MS,
                       limit_orders: float = BACKLOG_SLO_ORDERS) -> dict[str, float]:
    """SLOs per tenant: how many orders are waiting, and how long the oldest has waited."""
    thresholds = {}
    for name in snapshot.get("resources", {}):
        if name.startswith(TENANT_PREFIX):
            thresholds[f"resource.{name}.outstanding"] = limit_orders
            thresholds[f"resource.{name}.oldest_pending_ms"] = limit_ms
    return thresholds


def scope_size(spec: LeverSpec) -> int:
    """How many interchangeable parts of the system this lever can be pointed at.

    The control service reports every lever as 100% blast radius, which the orchestrator
    refuses outright, so no advanced experiment could ever run. A lever bounded to one of six
    tenants does not touch the other five; the catalog's own parameter schema says how many
    there are, so the estimate stays observable rather than hardcoded.
    """
    properties = (spec.params_schema or {}).get("properties", {})
    for name in SCOPE_PARAMS:
        field = properties.get(name)
        if not isinstance(field, dict):
            continue
        if isinstance(field.get("enum"), list) and field["enum"]:
            return len(field["enum"])
        low, high = field.get("minimum"), field.get("maximum")
        if isinstance(low, int) and isinstance(high, int) and high >= low:
            return high - low + 1
    return 1


class AdvancedLeverAdapter:
    """C3 over the advanced control service. Only reversible, TTL-bound levers."""

    def __init__(self, base_url: str, token: str, timeout_s: float = 10.0,
                 http: Callable[..., tuple[int, Any]] = _request):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_s = timeout_s
        self._http = http
        self._specs: dict[str, LeverSpec] | None = None

    def catalog(self) -> list[LeverSpec]:
        if self._specs is None:
            status, body = self._call("GET", "/catalog")
            if status != 200 or not isinstance(body, list):
                raise LeverError(f"advanced catalog unavailable (HTTP {status})")
            self._specs = {spec.id: spec for spec in (LeverSpec.model_validate(item) for item in body)}
        return list(self._specs.values())

    def estimate_blast_radius(self, lever_id: str, params: dict[str, Any]) -> float:
        spec = self._spec(lever_id)
        validate_params(spec, params, ttl_s=1)
        return 100.0 / scope_size(spec)

    def apply(self, lever_id: str, params: dict[str, Any], ttl_s: int) -> ActionHandle:
        spec = self._spec(lever_id)
        validate_params(spec, params, ttl_s=ttl_s)
        status, body = self._call("POST", "/actions", {
            "lever_id": lever_id, "params": params, "ttl_s": ttl_s, "incident_id": "faultline"})
        if status != 201:
            raise LeverError(f"{lever_id} refused (HTTP {status}): {str(body)[:200]}")
        return ActionHandle.model_validate(body)

    def undo(self, handle: ActionHandle) -> ActionHandle:
        status, body = self._call("DELETE", f"/actions/{handle.action_id}")
        if status != 200:
            raise LeverError(f"release of {handle.lever_id} failed (HTTP {status}): {str(body)[:200]}")
        return ActionHandle.model_validate(body) if isinstance(body, dict) and body.get("action_id") \
            else handle.model_copy(update={"status": ActionStatus.undone})

    def status(self, handle: ActionHandle) -> ActionStatus:
        status, body = self._call("GET", f"/actions/{handle.action_id}")
        if status == 404:
            return ActionStatus.expired
        if status != 200 or not isinstance(body, dict):
            raise LeverError(f"status of {handle.lever_id} unavailable (HTTP {status})")
        return ActionStatus(body.get("status", ActionStatus.active.value))

    def _spec(self, lever_id: str) -> LeverSpec:
        specs = {spec.id: spec for spec in self.catalog()}
        spec = specs.get(lever_id)
        if spec is None:
            raise LeverError(f"unknown lever {lever_id!r}")
        return spec

    def _call(self, method: str, path: str, data: dict | None = None) -> tuple[int, Any]:
        url = f"{self._base_url}{path}"
        try:
            return self._http(method, url, self._token, data, self._timeout_s)
        except (OSError, http.client.HTTPException) as error:
            raise LeverError(f"advanced control unreachable at {url}: {error}") from error


class AdvancedTelemetrySource:
    """Polls `/snapshot` and serves C1 windows built by Owner 2's distributed converter."""

    def __init__(self, base_url: str, token: str, timeout_s: float = 10.0, retain_s: float = 1800,
                 backlog_slo_ms: float = BACKLOG_SLO_MS,
                 http: Callable[..., tuple[int, Any]] = _request,
                 writer: FingerprintWriter | None = None,
                 incident_id: str | None = None, clone_id: str | None = None):
        self._url = base_url.rstrip("/") + "/snapshot"
        self._token = token
        self._timeout_s = timeout_s
        self._retain_s = retain_s
        self._backlog_slo_ms = backlog_slo_ms
        self._http = http
        self._writer = writer
        self._incident_id = incident_id
        self._clone_id = clone_id
        self._snapshots: deque[tuple[datetime, dict]] = deque()
        self._persisted: set[tuple[datetime, datetime]] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        try:
            status, body = self._http("GET", self._url, self._token, None, self._timeout_s)
        except (OSError, http.client.HTTPException) as error:
            raise TelemetryUnavailable(f"advanced telemetry unavailable at {self._url}: {error}") from error
        if status != 200 or not isinstance(body, dict) or "instances" not in body:
            raise TelemetryUnavailable(f"advanced telemetry returned HTTP {status}")
        observed = datetime.now(timezone.utc)
        with self._lock:
            self._snapshots.append((observed, body))
            cutoff = observed - timedelta(seconds=self._retain_s)
            while self._snapshots and self._snapshots[0][0] < cutoff:
                self._snapshots.popleft()
        return body

    def window(self, start: datetime, end: datetime) -> Fingerprint:
        with self._lock:
            entries = list(self._snapshots)
        if len(entries) < 2:
            raise ValueError(f"no telemetry covering [{start}, {end})")
        previous = next((i for i in range(len(entries) - 1, -1, -1) if entries[i][0] <= start), 0)
        current = next((i for i in range(len(entries) - 1, -1, -1) if entries[i][0] <= end), None)
        if current is None or previous == current:
            raise ValueError(f"no telemetry covering [{start}, {end})")
        fingerprint = fingerprint_from_distributed(
            entries[previous][1], entries[current][1], start, end,
            backlog_thresholds(entries[current][1], self._backlog_slo_ms))
        self._persist(fingerprint)
        return fingerprint

    def series(self, start: datetime, end: datetime, step_s: int = WINDOW_S) -> list[Fingerprint]:
        if step_s <= 0:
            raise ValueError("step_s must be positive")
        step = timedelta(seconds=step_s)
        out, cursor = [], start
        while cursor + step <= end + timedelta(seconds=0.5):
            try:
                out.append(self.window(cursor, cursor + step))
            except ValueError:
                pass
            cursor += step
        return out

    def latest(self) -> Fingerprint | None:
        with self._lock:
            if len(self._snapshots) < 2:
                return None
            (start, previous), (end, current) = list(self._snapshots)[-2:]
        fingerprint = fingerprint_from_distributed(
            previous, current, start, end, backlog_thresholds(current, self._backlog_slo_ms))
        self._persist(fingerprint)
        return fingerprint

    def wait_for_breach(self, timeout_s: float, poll_s: float = 1.0, sustain_s: float = 0) -> Fingerprint:
        """Latest fingerprint once an SLO has stayed breached for `sustain_s`; one bad window is a blip."""
        if timeout_s <= 0:
            raise TimeoutError(f"no SLO breach observed within {timeout_s}s")
        deadline = time.monotonic() + timeout_s
        breached_since: float | None = None
        while True:
            fingerprint = self.latest()
            now = time.monotonic()
            if fingerprint is not None and any(slo.breached for slo in fingerprint.slos):
                breached_since = now if breached_since is None else breached_since
                if now - breached_since >= sustain_s:
                    return fingerprint
            else:
                breached_since = None
            remaining = deadline - now
            if remaining <= 0:
                raise TimeoutError(f"no SLO breach observed within {timeout_s}s")
            self._stop.wait(min(max(poll_s, 0.01), remaining))

    def healthz(self) -> bool:
        try:
            self.snapshot()
        except TelemetryUnavailable:
            return False
        return True

    def start(self, period_s: float = WINDOW_S) -> None:
        if period_s <= 0:
            raise ValueError("period_s must be positive")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(period_s,), daemon=True,
                                            name="faultline-advanced-telemetry")
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop.set()
        thread.join()
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def _persist(self, fingerprint: Fingerprint) -> None:
        if self._writer is None:
            return
        identity = (fingerprint.window_start, fingerprint.window_end)
        with self._lock:
            if identity in self._persisted:
                return
            self._persisted.add(identity)
        try:
            self._writer.write(fingerprint, incident_id=self._incident_id, clone_id=self._clone_id)
        except Exception as exc:  # noqa: BLE001 - persistence must never break detection
            log.warning("advanced fingerprint persist failed (%s)", type(exc).__name__)
            with self._lock:
                self._persisted.discard(identity)

    def _run(self, period_s: float) -> None:
        while not self._stop.is_set():
            try:
                self.snapshot()
            except TelemetryUnavailable:
                pass
            except Exception:  # noqa: BLE001 - the poller must outlive any bad poll
                log.exception("advanced telemetry poll failed")
            self._stop.wait(period_s)


def advanced_sources(base_url: str, token: str, **kwargs) -> tuple[AdvancedTelemetrySource, AdvancedLeverAdapter]:
    return AdvancedTelemetrySource(base_url, token, **kwargs), AdvancedLeverAdapter(base_url, token)


__all__ = ["AdvancedLeverAdapter", "AdvancedTelemetrySource", "advanced_sources",
           "backlog_thresholds", "scope_size", "BACKLOG_SLO_MS"]
