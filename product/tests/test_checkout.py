from pathlib import Path

import pytest

from faultline_product.adapters.checkout import FixturePatchCheckout, GitPatchCheckout
from faultline_product.ports import CanaryPreparationError, PatchProposal


class FakeGit:
    def __init__(self, workdir: Path, sha: str = "abc123"):
        self.calls: list[tuple[list[str], Path]] = []
        self.sha = sha
        self._workdir = workdir

    def __call__(self, args, *, cwd):
        self.calls.append((args, cwd))
        if args[0] == "rev-parse":
            return self.sha
        if args[0] == "worktree" and args[1] == "add":
            target = Path(args[-2])
            (target / ".git").mkdir(parents=True)
            (target / "sandbox").mkdir(parents=True, exist_ok=True)
            (target / "sandbox" / "Dockerfile").write_text("FROM x")
        return ""


def _patch(reference):
    return PatchProposal("devin", reference, "fix")


def test_fixture_checkout_resolves_nothing():
    assert FixturePatchCheckout().resolve(_patch("devin://task/x")) is None


def test_override_wins(tmp_path):
    checkout = GitPatchCheckout(tmp_path, tmp_path / "wt", override=tmp_path / "patched")
    assert checkout.resolve(_patch("https://github.com/o/r/pull/7")) == (tmp_path / "patched").resolve()


def test_branch_reference_fetches_and_adds_worktree(tmp_path):
    git = FakeGit(tmp_path / "wt")
    checkout = GitPatchCheckout(tmp_path, tmp_path / "wt", git=git)

    path = checkout.resolve(_patch("branch:faultline/fallback-retry-cap"))

    assert path == (tmp_path / "wt" / "branch-faultline-fallback-retry-cap").resolve()
    fetch = git.calls[0][0]
    assert fetch[:2] == ["fetch", "--quiet"] and fetch[-1] == "faultline/fallback-retry-cap"
    assert any(call[0][:2] == ["worktree", "add"] for call in git.calls)


def test_pr_url_fetches_pull_head_and_refreshes_existing_worktree(tmp_path):
    git = FakeGit(tmp_path / "wt")
    checkout = GitPatchCheckout(tmp_path, tmp_path / "wt", git=git)
    first = checkout.resolve(_patch("https://github.com/Jay-Thpr/HackMIT/pull/42"))
    assert first.name == "pr-42"
    assert git.calls[0][0][-1] == "pull/42/head"

    git.calls.clear()
    git.sha = "def456"  # Devin pushed a revision to the same PR
    second = checkout.resolve(_patch("https://github.com/Jay-Thpr/HackMIT/pull/42"))
    assert second == first
    ops = [call[0][0] for call in git.calls]
    assert ops == ["fetch", "rev-parse", "checkout"]  # re-fetch + detach onto the new sha, no new worktree
    assert git.calls[-1][0][-1] == "def456" and git.calls[-1][1] == first


def test_unknown_reference_is_unresolvable_and_bad_dir_is_refused(tmp_path):
    checkout = GitPatchCheckout(tmp_path, tmp_path / "wt", git=FakeGit(tmp_path / "wt"))
    assert checkout.resolve(_patch("devin://task/x")) is None
    (tmp_path / "plain").mkdir()
    with pytest.raises(CanaryPreparationError, match="sandbox/Dockerfile"):
        checkout.resolve(_patch(f"path:{tmp_path / 'plain'}"))


def test_git_failure_becomes_preparation_error(tmp_path):
    def failing(args, *, cwd):
        raise CanaryPreparationError("git fetch failed: could not read from remote")

    checkout = GitPatchCheckout(tmp_path, tmp_path / "wt", git=failing)
    with pytest.raises(CanaryPreparationError, match="remote"):
        checkout.resolve(_patch("branch:nope"))
