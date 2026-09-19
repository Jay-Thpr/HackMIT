"""Resolve a PatchProposal.reference to a local checkout the sandbox can build orders-v2 from.

Supported references:
  path:/abs/dir                       an existing checkout root (or a bare existing directory)
  branch:<name>                       a branch on `origin` (the prebuilt fallback patch)
  https://github.com/<o>/<r>/pull/N   a GitHub PR (Devin's output); fetched as refs/pull/N/head

Each reference gets its own detached worktree under ``workdir``; re-resolving the same
reference re-fetches and fast-forwards the worktree, which is how a Devin revision (new
commits on the same PR) reaches the canary and the clone lab.
"""

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from ..ports import CanaryPreparationError, PatchCheckout, PatchProposal

_PR_URL = re.compile(r"^https?://github\.com/[^/]+/[^/]+/pull/(\d+)")


def _git(args: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise CanaryPreparationError(f"git {' '.join(args[:2])} failed: {detail.strip()}") from exc
    return result.stdout.strip()


class FixturePatchCheckout:
    def resolve(self, patch: PatchProposal) -> Path | None:
        del patch
        return None


class GitPatchCheckout(PatchCheckout):
    def __init__(
        self,
        repo_root: Path,
        workdir: Path,
        override: Path | None = None,
        remote: str = "origin",
        git: Callable[..., str] = _git,
    ):
        self._repo_root = repo_root
        self._workdir = workdir
        self._override = override
        self._remote = remote
        self._git = git

    def resolve(self, patch: PatchProposal) -> Path | None:
        if self._override is not None:
            return self._override.expanduser().resolve()
        reference = patch.reference
        if reference.startswith("path:"):
            return self._existing(Path(reference[5:]))
        if reference.startswith("branch:"):
            name = reference[7:]
            return self._worktree(f"branch-{_slug(name)}", name)
        match = _PR_URL.match(reference)
        if match:
            number = match.group(1)
            return self._worktree(f"pr-{number}", f"pull/{number}/head")
        if Path(reference).expanduser().is_dir():
            return self._existing(Path(reference))
        return None

    def _existing(self, path: Path) -> Path:
        path = path.expanduser().resolve()
        if not (path / "sandbox" / "Dockerfile").exists():
            raise CanaryPreparationError(f"{path} is not a checkout root (no sandbox/Dockerfile)")
        return path

    def _worktree(self, slot: str, refspec: str) -> Path:
        self._git(["fetch", "--quiet", self._remote, refspec], cwd=self._repo_root)
        sha = self._git(["rev-parse", "FETCH_HEAD"], cwd=self._repo_root)
        target = self._workdir / slot
        if (target / ".git").exists():
            self._git(["checkout", "--quiet", "--detach", sha], cwd=target)
        else:
            self._workdir.mkdir(parents=True, exist_ok=True)
            self._git(["worktree", "prune"], cwd=self._repo_root)
            self._git(["worktree", "add", "--quiet", "--detach", str(target), sha], cwd=self._repo_root)
        return self._existing(target)


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")[:48] or "ref"


__all__ = ["FixturePatchCheckout", "GitPatchCheckout"]
