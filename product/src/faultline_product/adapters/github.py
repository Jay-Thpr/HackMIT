"""GitHub pull-request adapter: Faultline opens the durable fix itself.

Used when Devin is not in the loop (``--patch github``). The fix is a prebuilt, reviewed
patch (``product/patches/bounded-retries.patch``: bounded retries with backoff, jitter,
a retry budget and deadline propagation across the checkout path); what this adapter adds
per incident is the branch, the commit and a pull request whose body carries the measured
evidence that justified it. ``revise`` is not supported: nobody is on the other end.

Flow: fetch ``base`` -> detached worktree -> ``git apply`` -> commit as Faultline -> push
``faultline/<incident>-bounded-retries`` -> ``POST /repos/{o}/{r}/pulls``. Any failure falls
back to the prebuilt branch reference so the incident loop never stalls on GitHub.
"""

from __future__ import annotations

import base64
import json
import shutil
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from faultline_contracts import TriageResult, Verdict

from ..paths import PRODUCT_ROOT, REPOSITORY_ROOT
from ..ports import PatchProposal
from .devin import _http_request

API = "https://api.github.com"
DEFAULT_PATCH = PRODUCT_ROOT / "patches" / "bounded-retries.patch"
AUTHOR = {"GIT_AUTHOR_NAME": "Faultline", "GIT_AUTHOR_EMAIL": "faultline@users.noreply.github.com",
          "GIT_COMMITTER_NAME": "Faultline", "GIT_COMMITTER_EMAIL": "faultline@users.noreply.github.com"}


class GitHubError(RuntimeError):
    pass


def _git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=120,
            env={**os.environ, **(env or {})},
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        shown = [a for i, a in enumerate(args) if a != "-c" and (i == 0 or args[i - 1] != "-c")]
        raise GitHubError(f"git {' '.join(shown[:2])} failed: {detail.strip()}") from exc
    return result.stdout.strip()


class GitHubPatchAdapter:
    def __init__(
        self,
        token: str | None,
        repo: str,
        fallback: PatchProposal,
        *,
        repo_root: Path = REPOSITORY_ROOT,
        patch_file: Path = DEFAULT_PATCH,
        base: str = "main",
        remote: str = "origin",
        workdir: Path | None = None,
        git: Callable[..., str] = _git,
        http: Callable[..., tuple[int, dict]] = _http_request,
    ):
        self._token, self._repo, self._fallback = token, repo, fallback
        self._repo_root, self._patch_file = repo_root, patch_file
        self._base, self._remote = base, remote
        self._workdir = workdir or repo_root / ".faultline" / "worktrees"
        self._git, self._http = git, http

    @property
    def enabled(self) -> bool:
        return bool(self._token) and self._patch_file.exists()

    # -- PatchAdapter -----------------------------------------------------------------------

    def propose(self, incident_id: str, verdict: Verdict, triage: TriageResult) -> PatchProposal:
        if not self.enabled:
            return self._fallback
        try:
            branch = f"faultline/{_slug(incident_id)}-bounded-retries"
            sha = self._commit(incident_id, verdict, branch)
            url, number = self._open_pull_request(incident_id, verdict, triage, branch)
        except Exception as exc:  # noqa: BLE001 - the loop must not stall on GitHub
            return PatchProposal(
                "fallback", self._fallback.reference,
                f"{self._fallback.summary}; GitHub PR not opened: {type(exc).__name__}: {exc}",
            )
        return PatchProposal(
            "github", url,
            f"PR #{number}: bounded retries with backoff, jitter, a retry budget and deadline "
            f"propagation across gateway, api, cache, inventory, payments and primary-db ({sha[:8]})",
        )

    def revise(self, incident_id: str, patch: PatchProposal, evidence: str) -> PatchProposal | None:
        del incident_id, patch, evidence
        return None

    # -- git ----------------------------------------------------------------------------------

    def _commit(self, incident_id: str, verdict: Verdict, branch: str) -> str:
        root = self._repo_root
        self._git(["fetch", "--quiet", self._remote, self._base], cwd=root)
        base_sha = self._git(["rev-parse", "FETCH_HEAD"], cwd=root)
        target = self._workdir / f"pr-{_slug(incident_id)}"
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._git(["worktree", "prune"], cwd=root)
        if target.exists():
            # Git 2.15 has `worktree prune` but not `worktree remove`. This
            # directory belongs solely to this adapter and has a deterministic
            # path below `self._workdir`, so removing it before pruning is safe.
            shutil.rmtree(target)
            self._git(["worktree", "prune"], cwd=root)
        # `worktree add --quiet` is not available in Git 2.15, which is still
        # common on developer machines. The command's normal output is never
        # surfaced to the incident report, so quietness is not worth losing PR
        # creation on those installations.
        self._git(["worktree", "add", "--detach", str(target), base_sha], cwd=root)
        try:
            self._git(["apply", "--index", str(self._patch_file)], cwd=target)
            self._git(["commit", "--quiet", "-m", _commit_message(incident_id, verdict)], cwd=target, env=AUTHOR)
            sha = self._git(["rev-parse", "HEAD"], cwd=target)
            self._git([*self._auth_config(), "push", "--quiet", "--force", self._remote, f"HEAD:refs/heads/{branch}"], cwd=target)
        finally:
            if target.exists():
                shutil.rmtree(target)
            self._git(["worktree", "prune"], cwd=root)
        return sha

    def _auth_config(self) -> list[str]:
        """Authenticate this push with the token, without writing it into any git config."""
        basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]

    # -- GitHub API ---------------------------------------------------------------------------

    def _open_pull_request(self, incident_id: str, verdict: Verdict, triage: TriageResult, branch: str) -> tuple[str, int]:
        owner, name = self._owner_repo()
        pulls = f"{API}/repos/{owner}/{name}/pulls"
        status, body = self._http(
            "POST", pulls, headers=self._headers(),
            data=json.dumps({
                "title": f"Faultline: bound retries across the checkout path ({incident_id})",
                "head": branch, "base": self._base, "body": pull_request_body(incident_id, verdict, triage),
            }).encode(),
        )
        if status == 422:  # a PR for this head already exists (re-run of the same incident)
            status, listed = self._http("GET", f"{pulls}?head={owner}:{branch}&state=open", headers=self._headers())
            if status < 400 and listed:
                body = listed[0] if isinstance(listed, list) else listed
            else:
                raise GitHubError(f"pull request exists but could not be listed ({status})")
        elif status >= 400:
            raise GitHubError(f"POST pulls -> {status}: {body.get('message', '')}".strip())
        return body["html_url"], int(body["number"])

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "faultline",
        }

    def _owner_repo(self) -> tuple[str, str]:
        slug = self._repo
        for prefix in ("https://", "http://", "github.com/"):
            if slug.startswith(prefix):
                slug = slug[len(prefix):]
        owner, name = slug.removesuffix(".git").strip("/").split("/")[:2]
        return owner, name


class PatchFallbackChain:
    """Ask ``primary`` (Devin) first; if it only hands back the static fallback, let ``secondary``
    (this adapter) open the pull request instead. Revisions go to whoever authored the patch."""

    def __init__(self, primary, secondary):
        self._primary, self._secondary = primary, secondary

    def propose(self, incident_id: str, verdict: Verdict, triage: TriageResult) -> PatchProposal:
        patch = self._primary.propose(incident_id, verdict, triage)
        if patch.provider != "fallback":
            return patch
        opened = self._secondary.propose(incident_id, verdict, triage)
        if opened.provider == "fallback":
            return patch  # keep the primary's explanation of what went wrong
        return PatchProposal(opened.provider, opened.reference, f"{opened.summary}; {patch.summary}")

    def revise(self, incident_id: str, patch: PatchProposal, evidence: str) -> PatchProposal | None:
        if patch.provider == "github":
            return self._secondary.revise(incident_id, patch, evidence)
        return self._primary.revise(incident_id, patch, evidence)


# -- text ---------------------------------------------------------------------------------------

CHANGES = [
    ("gateway", "demo/shop/gateway/envoy.yaml",
     "retries only on connection failure, one retry, 10 % retry budget, 2 s end-to-end route timeout, "
     "backoff 50–400 ms; retry concurrency circuit breaker"),
    ("api", "demo/shop/api/retry_policy.py, demo/shop/api/checkout.py",
     "max_retries 3 → 1 drawn from a token-bucket retry budget (0.1 tokens/request), full-jitter "
     "exponential backoff, 1.5 s request deadline propagated downstream as x-request-deadline-ms"),
    ("cache", "demo/shop/cache/redis.yaml",
     "max_retries 3 → 1 with backoff, 200 ms timeouts, in-flight miss coalescing, negative TTL, "
     "stale-while-revalidate so a cold cache cannot stampede primary-db"),
    ("inventory", "demo/shop/inventory/db.py",
     "lock retries 5 → 1 with jittered backoff inside the caller's deadline; SET LOCAL "
     "statement_timeout/lock_timeout per transaction; pool max 64 → 32"),
    ("payments", "demo/shop/payments/processor_client.py",
     "hedged duplicate request removed; max_retries 3 → 1, retry budget, backoff, per-charge deadline"),
    ("primary-db", "demo/shop/primary-db/postgresql.conf, demo/shop/primary-db/pgbouncer.ini",
     "statement_timeout 0 → 2 s, lock_timeout 0 → 500 ms, idle-in-transaction 5 s, pgbouncer "
     "query_wait_timeout 0 → 2 s; lock waits logged"),
    ("orders (sandbox)", "sandbox/services/orders/app.py",
     "the same bounded policy in the service Faultline canaries as orders-v2: max_retries 3 → 1, "
     "retry budget, backoff with jitter; runtime retry override endpoint unchanged"),
]


def pull_request_body(incident_id: str, verdict: Verdict, triage: TriageResult) -> str:
    hypothesis = next((h for h in triage.hypotheses if h.id == verdict.diagnosis), None)
    label = hypothesis.label if hypothesis else verdict.diagnosis
    description = hypothesis.description if hypothesis else ""
    rejected = [h for h in triage.hypotheses if h.id != verdict.diagnosis]
    experiments = sorted({o.experiment_id for o in verdict.observations})
    lines = [
        f"Opened by Faultline for incident `{incident_id}`.",
        "",
        "## Diagnosis",
        f"**{label}** (`{verdict.diagnosis}`) — {description}".rstrip(" —"),
        "",
        f"> {verdict.summary}",
        "",
    ]
    if rejected:
        lines += ["Ruled out: " + "; ".join(f"**{h.label}** (`{h.id}`)" for h in rejected), ""]
    lines += [
        "## Evidence",
        f"Live experiment{'s' if len(experiments) != 1 else ''}: " + ", ".join(f"`{e}`" for e in experiments)
        + ". Each metric is judged against the healthy baseline's noise (σ = max(std, 10 % of typical)); "
          "|z| ≥ 3 is significant.",
        "",
        "| phase | metric | baseline | measured | z | direction |",
        "|---|---|---:|---:|---:|---|",
    ]
    seen: set[tuple[str, str]] = set()
    for o in verdict.observations:
        key = (o.phase.value, o.metric)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"| {o.phase.value} | `{o.metric}` | {o.baseline:g} | {o.measured:g} | {o.z:+.1f} | {o.direction.value} |")
    lines += [
        "",
        "## What changed",
        "Every hop on the checkout path retried immediately and without a budget, so a transient "
        "slowdown became a self-sustaining retry storm that outlived its trigger. This bounds retries "
        "at each hop and makes them share one deadline:",
        "",
        "| service | files | change |",
        "|---|---|---|",
    ]
    lines += [f"| {svc} | {files} | {change} |" for svc, files, change in CHANGES]
    lines += [
        "",
        "## Safety",
        "- Behaviour under a healthy dependency is unchanged: a single attempt succeeds as before.",
        "- Under a slow dependency, load on it is capped at (1 + retry budget) × offered load instead of "
        "(1 + max_retries) × offered load per hop, compounded across hops.",
        "- The runtime `retry_cap` lever (the emergency mitigation currently holding production) is "
        "still honoured and superseded by this policy once deployed.",
        "- Rollback: revert this PR; no data migration, no schema change.",
        "",
        f"Audit trail: `faultline report --incident {incident_id}`. The LLM proposed the hypotheses; "
        "the measured experiment above decided between them.",
    ]
    return "\n".join(lines)


def _commit_message(incident_id: str, verdict: Verdict) -> str:
    return (
        "Bound retries across the checkout path (backoff, jitter, retry budget, deadlines)\n\n"
        f"Faultline incident {incident_id}: {verdict.diagnosis} confirmed by a live experiment.\n"
        f"{verdict.summary}\n\n"
        "Immediate, unbudgeted retries at gateway, api, cache, inventory, payments and the\n"
        "orders sandbox service let a transient slowdown become a self-sustaining retry storm.\n"
        "Each hop now retries at most once, from a retry budget, with full-jitter backoff,\n"
        "inside a propagated request deadline; primary-db bounds statements server-side."
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")[:48] or "incident"


__all__ = ["GitHubPatchAdapter", "GitHubError", "PatchFallbackChain", "pull_request_body", "DEFAULT_PATCH"]
