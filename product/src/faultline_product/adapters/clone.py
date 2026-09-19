"""C6 clone adapter: verify a patch by replaying the reproduced incident in a clean clone.

The clone is built only from observable config plus ``patch_ref`` (Owner 1's lab builds the
patched Orders as the clone's orders-v2). Everything here talks to the clone through the same
C1 (/stats) and C3 (control) surfaces production exposes, plus the clone-only C6 lab actions.
Production is never touched; the clone is destroyed on the way out, pass or fail.
"""

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from faultline_contracts import WINDOW_S, Fingerprint, LeverError, utcnow
from faultline_contracts.clone import CloneInfo, CloneLab, CloneSpec, CloneStatus, LabError

from ..ports import PatchProposal, PatchVerification, PatchVerifier, VerificationStatus
from .live_telemetry import FingerprintWriter, LiveTelemetrySource, TelemetryUnavailable
from .sandbox import SandboxLeverAdapter

# How each hero diagnosis is reproduced in a clone (sandbox/INTEGRATION.md "reproduction
# recipes"). Investigators (Owner 3) can pass their own measured recipe instead.
Recipe = dict[str, Any]
DEFAULT_RECIPES: dict[str, Recipe] = {
    "H_meta": {"action": "db_latency", "params": {"extra_ms": 800}, "ttl_s": 20},
    "H_db": {"action": "db_capacity", "params": {"capacity_qps": 40}, "ttl_s": 20},
}


class FixturePatchVerifier:
    def verify(
        self, incident_id: str, patch: PatchProposal, diagnosis: str, context: Path | None = None
    ) -> PatchVerification:
        del context
        return PatchVerification(
            VerificationStatus.passed,
            "fixture clone: replayed incident, SLO recovered",
            clone_id=f"fixture-{incident_id}",
            recipe=DEFAULT_RECIPES.get(diagnosis),
        )


class LabPatchVerifier(PatchVerifier):
    """Create clone(patch_ref) -> route all clone traffic to orders-v2 -> inject the recipe ->
    wait for it to expire and settle -> require the clone's checkout SLO to be healthy again.

    A retry-storm fix has exactly one job: once the trigger is gone the system must recover on
    its own. That is what is measured, against the clone's own SLO, over ``settle_s`` after the
    recipe expires. ``stress`` adds extra lab actions applied alongside the recipe (higher load,
    CPU squeeze) so the patch is attacked, not just replayed.
    """

    def __init__(
        self,
        lab: CloneLab,
        context: Path | None = None,
        recipes: dict[str, Recipe] | None = None,
        stress: list[Recipe] | None = None,
        settle_s: float = 30,
        healthy_windows: int = 4,
        telemetry_factory: Callable[[CloneInfo, str], LiveTelemetrySource] | None = None,
        levers_factory: Callable[[CloneInfo], SandboxLeverAdapter] | None = None,
        writer: FingerprintWriter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
    ):
        self._lab = lab
        self._context = context
        self._writer = writer  # Owner 2's ES store: clone windows land tagged with clone_id
        self._recipes = recipes or DEFAULT_RECIPES
        self._stress = stress or []
        self._settle_s = settle_s
        self._healthy_windows = healthy_windows
        self._telemetry_factory = telemetry_factory or self._clone_telemetry
        self._levers_factory = levers_factory or _clone_levers
        self._sleep = sleep
        self._clock = clock

    def verify(
        self,
        incident_id: str,
        patch: PatchProposal,
        diagnosis: str,
        context: Path | None = None,
    ) -> PatchVerification:
        recipe = self._recipes.get(diagnosis)
        if recipe is None:
            return PatchVerification(
                VerificationStatus.skipped, f"no reproduction recipe for {diagnosis}"
            )
        context = self._context or context
        if context is None:
            return PatchVerification(
                VerificationStatus.skipped,
                f"no checkout for {patch.reference}: nothing to build into the clone",
            )
        spec = CloneSpec(name=_clone_name(incident_id), patch_ref=str(context.expanduser().resolve()))
        try:
            clone = self._lab.create(spec)
        except (LabError, httpx.HTTPError) as exc:
            return PatchVerification(VerificationStatus.skipped, f"clone lab unavailable: {exc}")
        if clone.status != CloneStatus.ready or clone.endpoints is None:
            return PatchVerification(
                VerificationStatus.skipped, f"clone {clone.clone_id} not ready: {clone.detail}",
                clone_id=clone.clone_id,
            )
        try:
            return self._run(clone, recipe, incident_id)
        except (LabError, LeverError, TelemetryUnavailable, httpx.HTTPError) as exc:
            return PatchVerification(
                VerificationStatus.skipped, f"clone verification aborted: {exc}",
                clone_id=clone.clone_id, recipe=recipe,
            )
        finally:
            try:
                self._lab.destroy(clone.clone_id)
            except (LabError, httpx.HTTPError):
                pass

    def _run(self, clone: CloneInfo, recipe: Recipe, incident_id: str) -> PatchVerification:
        telemetry = self._telemetry_factory(clone, incident_id)
        levers = self._levers_factory(clone)
        telemetry.start()
        try:
            hold_s = recipe["ttl_s"] + self._settle_s + self._healthy_windows * WINDOW_S
            # All clone traffic goes to the patched orders-v2 for the whole replay.
            levers.apply("canary_weight", {"v2_weight": 1.0}, int(hold_s) + 60)
            self._sleep(2 * WINDOW_S)  # a couple of healthy windows on v2 before the replay
            handles = [self._lab.apply(clone.clone_id, recipe["action"], recipe["params"], recipe["ttl_s"])]
            for extra in self._stress:
                handles.append(
                    self._lab.apply(clone.clone_id, extra["action"], extra["params"], extra["ttl_s"])
                )
            injected_at = self._clock()
            self._sleep(recipe["ttl_s"] + self._settle_s)
            settled_at = self._clock()
            self._sleep(self._healthy_windows * WINDOW_S)
            end = self._clock()
            for handle in handles:
                try:
                    self._lab.undo(handle)
                except LabError:
                    pass  # already expired: that is the point of the ttl
            during = telemetry.series(injected_at, settled_at)
            after = telemetry.series(settled_at, end)
        finally:
            telemetry.stop()

        evidence = {
            "incident_reproduced": _any_breached(during),
            "windows_after_settle": len(after),
            "breached_after_settle": sum(_breached(fp) for fp in after),
            "orders_v2_p99_ms_after": _mean(fp.services["orders_v2"].p99_ms for fp in after if "orders_v2" in fp.services),
            "gateway_p99_ms_after": _mean(fp.services["gateway"].p99_ms for fp in after if "gateway" in fp.services),
            "retry_ratio_after": _mean(fp.services["orders_v2"].retry_ratio for fp in after if "orders_v2" in fp.services),
        }
        if not after:
            return PatchVerification(
                VerificationStatus.failed, "no clone telemetry after the replay",
                clone_id=clone.clone_id, recipe=recipe, evidence=evidence,
            )
        if evidence["breached_after_settle"]:
            return PatchVerification(
                VerificationStatus.failed,
                f"clone SLO still breached in {evidence['breached_after_settle']}/{len(after)} windows "
                f"{self._settle_s:g}s after the replayed trigger ended",
                clone_id=clone.clone_id, recipe=recipe, evidence=evidence,
            )
        note = "" if evidence["incident_reproduced"] else " (trigger never breached the SLO on the patched build)"
        return PatchVerification(
            VerificationStatus.passed,
            f"clone recovered on its own: {len(after)}/{len(after)} healthy windows after the replay{note}",
            clone_id=clone.clone_id, recipe=recipe, evidence=evidence,
        )

    def _clone_telemetry(self, clone: CloneInfo, incident_id: str) -> LiveTelemetrySource:
        urls = clone.endpoints.stats_urls
        return LiveTelemetrySource(
            orders_url=urls["orders"],
            payments_url=urls["payments"],
            loadgen_url=urls["loadgen"],
            orders_v2_url=urls.get("orders-v2"),
            writer=self._writer,
            incident_id=incident_id,
            clone_id=clone.clone_id,
        )


def _clone_name(incident_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in incident_id.lower())
    return f"verify-{safe}"[:32].rstrip("-") or "verify"


def _clone_levers(clone: CloneInfo) -> SandboxLeverAdapter:
    return SandboxLeverAdapter(base_url=clone.endpoints.control_url)


def _breached(fp: Fingerprint) -> bool:
    return any(slo.breached for slo in fp.slos)


def _any_breached(fps: list[Fingerprint]) -> bool:
    return any(_breached(fp) for fp in fps)


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


__all__ = ["DEFAULT_RECIPES", "FixturePatchVerifier", "LabPatchVerifier"]
