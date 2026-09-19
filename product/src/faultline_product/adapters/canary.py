import http.client
import json
import os
import subprocess
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..ports import CanaryPreparationError, CanaryTarget, PatchProposal


def _get_json(url: str, timeout_s: float) -> dict[str, Any]:
    # A container whose port is bound before uvicorn listens resets the connection
    # (RemoteDisconnected); treat every transport failure as "not ready yet".
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            return json.loads(response.read())
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        raise CanaryPreparationError(f"canary target unavailable at {url}") from exc


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    try:
        subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = (getattr(exc, "stderr", "") or "").strip().splitlines()
        tail = " | ".join(stderr[-3:]) if stderr else str(exc)
        raise CanaryPreparationError(f"orders-v2 build or startup failed: {tail}") from exc


def _revision(context: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=context,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CanaryPreparationError("--canary-context must be a Git checkout") from exc
    return result.stdout.strip()


class FixtureCanaryDeployer:
    def prepare(self, patch: PatchProposal, context: Path | None = None) -> CanaryTarget:
        del context
        return CanaryTarget(
            patch_reference=patch.reference,
            version="fixture-v2",
            source_revision="fixture",
        )


class SandboxCanaryDeployer:
    """Build orders-v2 from the patch's checkout and wait for readiness.

    ``context`` is an operator override (--canary-context); otherwise the checkout resolved
    from the patch reference is used.
    """

    def __init__(
        self,
        compose_dir: Path,
        context: Path | None = None,
        orders_v2_url: str = "http://127.0.0.1:8104",
        timeout_s: float = 90,
        poll_s: float = 1,
        runner: Callable[..., None] = _run,
        http: Callable[[str, float], dict[str, Any]] = _get_json,
        revision: Callable[[Path], str] = _revision,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._compose_dir = compose_dir
        self._context = context
        self._orders_v2_url = orders_v2_url.rstrip("/")
        self._timeout_s = timeout_s
        self._poll_s = poll_s
        self._runner = runner
        self._http = http
        self._revision = revision
        self._sleep = sleep

    def prepare(self, patch: PatchProposal, context: Path | None = None) -> CanaryTarget:
        context = self._context or context
        if context is None:
            raise CanaryPreparationError(
                f"no checkout for {patch.reference}: pass --canary-context or a resolvable patch reference"
            )
        context = context.expanduser().resolve()
        if not context.is_dir():
            raise CanaryPreparationError(f"canary context does not exist: {context}")
        source_revision = self._revision(context)
        env = dict(os.environ)
        env["ORDERS_V2_CONTEXT"] = str(context)
        # --no-deps: orders-v2 depends_on payments; without it compose recreates production
        # payments whenever the shared image changes, which is a production incident of its own.
        self._runner(
            ["docker", "compose", "--profile", "canary", "up", "-d", "--build", "--no-deps", "orders-v2"],
            cwd=self._compose_dir,
            env=env,
        )

        deadline = time.monotonic() + self._timeout_s
        last_error: CanaryPreparationError | None = None
        while time.monotonic() < deadline:
            try:
                health = self._http(f"{self._orders_v2_url}/healthz", 3)
                if health.get("ok") and health.get("version") == "v2":
                    return CanaryTarget(
                        patch_reference=patch.reference,
                        version="v2",
                        source_revision=source_revision,
                        service_name="orders_v2",
                    )
            except CanaryPreparationError as exc:
                last_error = exc
            self._sleep(self._poll_s)
        detail = f": {last_error}" if last_error else ""
        raise CanaryPreparationError(f"orders-v2 did not become ready{detail}")
