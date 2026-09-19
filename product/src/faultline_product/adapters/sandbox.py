import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from faultline_contracts import (
    CATALOG,
    ActionHandle,
    ActionStatus,
    LeverError,
    LeverSpec,
    UndoSpec,
    standard_blast_radius,
    utcnow,
)

from .fixture import validate_params

PATHS = {
    "retry_cap": "retry_override",
    "shed": "shed",
    "db_failover": "db/failover",
    "canary_weight": "canary",
}


def _http_request(
    method: str,
    url: str,
    *,
    data: bytes | None = None,
    timeout: float,
) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        body = error.read()
        return error.code, json.loads(body) if body else {}
    except urllib.error.URLError as error:
        raise LeverError(f"control service unreachable at {url}: {error}") from error


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class SandboxLeverAdapter:
    def __init__(
        self,
        base_url: str = "http://localhost:9901",
        timeout_s: float = 5.0,
        clock: Callable[[], datetime] = utcnow,
        http: Callable[..., tuple[int, dict]] = _http_request,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._clock = clock
        self._http = http
        self._specs = {spec.id: spec for spec in CATALOG}
        self._undone: set[str] = set()

    def catalog(self) -> list[LeverSpec]:
        return list(CATALOG)

    def estimate_blast_radius(self, lever_id: str, params: dict[str, Any]) -> float:
        spec = self._specs.get(lever_id)
        if spec is None:
            raise LeverError(f"unknown lever {lever_id!r}")
        validate_params(spec, params, ttl_s=1)
        return standard_blast_radius(lever_id, params)

    def apply(self, lever_id: str, params: dict[str, Any], ttl_s: int) -> ActionHandle:
        path = PATHS.get(lever_id)
        spec = self._specs.get(lever_id)
        if path is None or spec is None:
            raise LeverError(f"unknown lever {lever_id!r}")
        validate_params(spec, params, ttl_s)
        status, body = self._request(
            "POST",
            f"/admin/{path}",
            data={**params, "ttl_s": ttl_s},
        )
        self._raise_for_status(lever_id, status, body)
        applied_at = _parse_iso(body.get("applied_at")) or self._clock()
        return ActionHandle(
            action_id=uuid4().hex,
            lever_id=lever_id,
            params=dict(params),
            applied_at=applied_at,
            ttl_s=ttl_s,
            undo=UndoSpec(lever_id=lever_id, payload={"path": path}),
        )

    def undo(self, handle: ActionHandle) -> ActionHandle:
        path = handle.undo.payload["path"]
        status, body = self._request("DELETE", f"/admin/{path}")
        self._raise_for_status(handle.lever_id, status, body)
        self._undone.add(handle.action_id)
        return handle.model_copy(update={"status": ActionStatus.undone})

    def status(self, handle: ActionHandle) -> ActionStatus:
        if handle.action_id in self._undone:
            return ActionStatus.undone
        status, body = self._request("GET", "/admin/levers")
        self._raise_for_status(handle.lever_id, status, body)
        try:
            entry = body[handle.lever_id]
        except KeyError as error:
            raise LeverError(f"missing lever status for {handle.lever_id}") from error
        if entry["active"]:
            expires_at = _parse_iso(entry.get("expires_at"))
            if expires_at is None or abs((expires_at - handle.expires_at).total_seconds()) > 2:
                return ActionStatus.undone
            return ActionStatus.active
        expires_at = _parse_iso(entry.get("expires_at"))
        if expires_at is not None and expires_at <= self._clock():
            return ActionStatus.expired
        return ActionStatus.undone

    def healthz(self) -> bool:
        try:
            status, _ = self._request("GET", "/healthz")
        except LeverError:
            return False
        return status == 200

    def _request(self, method: str, path: str, data: dict | None = None) -> tuple[int, dict]:
        payload = json.dumps(data).encode() if data is not None else None
        url = f"{self._base_url}{path}"
        try:
            return self._http(method, url, data=payload, timeout=self._timeout_s)
        except urllib.error.URLError as error:
            raise LeverError(f"control service unreachable at {url}: {error}") from error

    @staticmethod
    def _raise_for_status(lever_id: str, status: int, body: dict) -> None:
        if not 200 <= status < 300:
            detail = body.get("detail", body)
            raise LeverError(f"{lever_id}: {status} {detail}")
