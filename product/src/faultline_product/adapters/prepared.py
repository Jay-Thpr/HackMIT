import hashlib
import json
import os
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from faultline_contracts.clone import CloneInfo, CloneLab, CloneStatus

from ..ports import (
    CanaryPreparationError,
    CanaryTarget,
    PatchProposal,
    PatchVerification,
    PatchVerifier,
    VerificationStatus,
)
from .canary import _get_json

MANIFEST = "prepared-patch.json"
SCHEMA = "faultline-prepared-patch/1"
REPLAY_PROFILE = "retry-storm-v1"
LABEL = "Prepared bounded-retry patch; not generated during this run"
SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache"}


def patch_digest(context: Path) -> str:
    raw = Path(context).expanduser()
    if raw.is_symlink():
        raise ValueError(f"refusing symlinked snapshot root: {raw}")
    context = raw.resolve()
    digest = hashlib.sha256()
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(context):
        for name in dirnames:
            if (Path(dirpath) / name).is_symlink():
                raise ValueError(f"refusing symlink in snapshot: {Path(dirpath) / name}")
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                raise ValueError(f"refusing symlink in snapshot: {path}")
            if path.is_file() and name != MANIFEST:
                files.append(path)
    for path in sorted(files):
        rel = path.relative_to(context).as_posix()
        digest.update(rel.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


class PreparedPatchAdapter:
    def __init__(self, context: Path):
        raw = Path(context).expanduser()
        if raw.is_symlink():
            raise ValueError(f"refusing symlinked snapshot root: {raw}")
        self.context = raw.resolve()
        self._digest: str | None = None

    @property
    def proposal(self) -> PatchProposal:
        self.validate()
        return PatchProposal(
            provider="prepared",
            reference=f"prepared:sha256:{self._digest}",
            summary=LABEL,
        )

    def validate(self) -> None:
        if not self.context.is_dir():
            raise ValueError(f"prepared patch context does not exist: {self.context}")
        manifest_path = self.context / MANIFEST
        if manifest_path.is_symlink():
            raise ValueError(f"prepared patch manifest is a symlink: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid prepared patch manifest at {manifest_path}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError("prepared patch manifest is not an object")
        if manifest.get("schema_version") != SCHEMA or manifest.get("provider") != "prepared":
            raise ValueError("prepared patch manifest has the wrong schema or provider")
        if manifest.get("replay_profile") != REPLAY_PROFILE or manifest.get("label") != LABEL:
            raise ValueError("prepared patch manifest is not the retry-storm-v1 profile")
        manifest_digest = manifest.get("content_sha256")
        actual = patch_digest(self.context)
        if manifest_digest != actual:
            raise ValueError("prepared patch snapshot changed since it was built (digest mismatch)")
        if self._digest is None:
            self._digest = actual
        elif actual != self._digest:
            raise ValueError("prepared patch identity changed during the run")

    def propose(self, incident_id, verdict, triage) -> PatchProposal:
        self.validate()
        if not (verdict.confirmed and verdict.diagnosis == "H_meta"):
            raise ValueError(
                f"prepared patch only serves a confirmed H_meta diagnosis, got "
                f"{verdict.diagnosis} (confirmed={verdict.confirmed})"
            )
        return self.proposal

    def revise(self, incident_id, patch, evidence):
        return None


class PreparedCanaryDeployer:
    def __init__(
        self,
        patches: PreparedPatchAdapter,
        target: CloneInfo,
        http: Callable[[str, float], dict[str, Any]] = _get_json,
        lab: CloneLab | None = None,
    ):
        self._patches = patches
        self._target = target
        self._http = http
        self._lab = lab

    def prepare(self, patch: PatchProposal, context: Path | None = None) -> CanaryTarget:
        try:
            proposal = self._patches.proposal
        except ValueError as exc:
            raise CanaryPreparationError(str(exc)) from exc
        if context is not None:
            raw = Path(context).expanduser()
            if raw.is_symlink() or raw.resolve() != self._patches.context:
                raise CanaryPreparationError(
                    "canary context is not the validated prepared snapshot")
        if patch.reference != proposal.reference:
            raise CanaryPreparationError(
                f"patch {patch.reference} is not the prepared proposal {proposal.reference}"
            )
        try:
            self._patches.validate()
        except ValueError as exc:
            raise CanaryPreparationError(str(exc)) from exc
        target = self._lab.get(self._target.clone_id) if self._lab is not None else self._target
        patch_ref = target.spec.patch_ref
        if patch_ref is None or Path(patch_ref).expanduser().resolve() != self._patches.context:
            raise CanaryPreparationError(
                f"clone {target.clone_id} was not built from the prepared snapshot"
            )
        if target.status != CloneStatus.ready or target.endpoints is None:
            raise CanaryPreparationError(
                f"canary target {target.clone_id} is {target.status.value}, not ready"
            )
        v2_url = target.endpoints.stats_urls.get("orders-v2")
        if v2_url is None:
            raise CanaryPreparationError(f"clone {target.clone_id} exposes no orders-v2 endpoint")
        health = self._http(f"{v2_url.rstrip('/')}/healthz", 3)
        if health.get("ok") is not True or health.get("version") != "v2":
            raise CanaryPreparationError("orders-v2 healthz is not the exact healthy v2")
        if health.get("prepared_sha256") != self._patches._digest:
            raise CanaryPreparationError(
                "orders-v2 running image was not built from this prepared snapshot"
            )
        return CanaryTarget(
            patch_reference=patch.reference,
            version="v2",
            source_revision=self._patches._digest,
            service_name="orders_v2",
        )


class ConcurrentPreparedVerifier:
    def __init__(
        self,
        verifier: PatchVerifier,
        patches: PreparedPatchAdapter,
        *,
        executor=None,
    ):
        self._verifier = verifier
        self._patches = patches
        self._executor = executor or ThreadPoolExecutor(max_workers=1)
        self._incident_id: str | None = None
        self._reference: str | None = None
        self._future: Future | None = None

    def start(self, incident_id: str) -> None:
        if self._future is not None:
            raise RuntimeError("prepared verification already started")
        self._patches.validate()
        proposal = self._patches.proposal
        self._incident_id = incident_id
        self._reference = proposal.reference
        self._future = self._executor.submit(
            self._verifier.verify,
            incident_id,
            proposal,
            "H_meta",
            self._patches.context,
        )

    def verify(
        self,
        incident_id: str,
        patch: PatchProposal,
        diagnosis: str,
        context: Path | None = None,
    ) -> PatchVerification:
        def failed(detail: str, result: PatchVerification | None = None) -> PatchVerification:
            return PatchVerification(
                VerificationStatus.failed, detail,
                clone_id=result.clone_id if result else None,
                recipe=result.recipe if result else None,
                evidence=result.evidence if result else None,
            )

        if self._future is None or incident_id != self._incident_id:
            return failed(f"no prepared replay was started for incident {incident_id}")
        if patch.reference != self._reference or diagnosis != "H_meta":
            return failed(
                f"verify call does not match the prepared replay ({patch.reference}, {diagnosis})"
            )
        try:
            current = self._patches.proposal.reference
        except ValueError as exc:
            return failed(f"prepared snapshot changed during the run: {exc}")
        if current != self._reference:
            return failed("prepared patch identity changed since the replay started")
        if context is not None and Path(context).expanduser().resolve() != self._patches.context:
            return failed("verify context is not the prepared snapshot")
        try:
            result = self._future.result()
        except Exception as exc:  # noqa: BLE001 - a crashed replay is a failed verification
            return failed(f"prepared replay raised {type(exc).__name__}: {exc}")
        try:
            self._patches.validate()
        except ValueError as exc:
            return failed(f"prepared snapshot changed during the run: {exc}", result)
        try:
            current = self._patches.proposal.reference
        except ValueError as exc:
            return failed(f"prepared snapshot changed during the run: {exc}", result)
        if current != self._reference:
            return failed("prepared patch identity changed during the replay", result)
        if result.status != VerificationStatus.passed:
            return failed(f"prepared replay did not pass: {result.detail}", result)
        return result

    def close(self) -> None:
        if self._future is not None:
            try:
                self._future.result()
            except Exception:  # noqa: BLE001 - surfaced by verify(); close only joins
                pass
        self._executor.shutdown(wait=True)


__all__ = [
    "ConcurrentPreparedVerifier",
    "PreparedCanaryDeployer",
    "PreparedPatchAdapter",
    "patch_digest",
]
