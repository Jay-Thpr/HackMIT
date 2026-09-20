import json
import subprocess

from faultline_product.adapters.github import DEFAULT_PATCH, GitHubPatchAdapter, PatchFallbackChain, pull_request_body
from faultline_product.fixtures import load_fixture
from faultline_product.ports import PatchProposal

REPO = "github.com/Jay-Thpr/HackMIT"
PULLS = "https://api.github.com/repos/Jay-Thpr/HackMIT/pulls"


def _fallback():
    return PatchProposal("fallback", "branch:faultline/fallback-retry-cap", "Prebuilt patch")


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo_with_remote(tmp_path):
    """A clone whose `origin` is a local bare repo; main carries the files the patch touches."""
    base_paths = [
        line.split()[-1]
        for line in DEFAULT_PATCH.read_text().splitlines()
        if line.startswith("--- a/")
    ]
    from faultline_product.paths import REPOSITORY_ROOT

    bare = tmp_path / "origin.git"
    _git("init", "--bare", "--quiet", "-b", "main", str(bare), cwd=tmp_path)
    work = tmp_path / "work"
    _git("clone", "--quiet", str(bare), str(work), cwd=tmp_path)
    for rel in base_paths:
        rel = rel[len("a/"):]
        src = REPOSITORY_ROOT / rel
        dst = work / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    _git("add", "-A", cwd=work)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "base", cwd=work)
    _git("push", "--quiet", "origin", "HEAD:main", cwd=work)
    return work, bare


class Script:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, *, headers, data=None):
        self.calls.append((method, url, json.loads(data) if data else None, headers))
        return self.responses.pop(0)


def test_opens_pull_request_from_prebuilt_patch(tmp_path):
    work, bare = _repo_with_remote(tmp_path)
    bundle = load_fixture("storm")
    http = Script([(201, {"html_url": "https://github.com/Jay-Thpr/HackMIT/pull/99", "number": 99})])
    adapter = GitHubPatchAdapter("ghp_test", REPO, _fallback(), repo_root=work, workdir=tmp_path / "wt", http=http)
    assert adapter.enabled

    patch = adapter.propose("demo-storm-1", bundle.verdict, bundle.triage)

    assert patch.provider == "github"
    assert patch.reference == "https://github.com/Jay-Thpr/HackMIT/pull/99"
    assert "PR #99" in patch.summary
    method, url, body, headers = http.calls[0]
    assert (method, url) == ("POST", PULLS)
    assert body["head"] == "faultline/demo-storm-1-bounded-retries" and body["base"] == "main"
    assert headers["Authorization"] == "Bearer ghp_test"
    assert "## Evidence" in body["body"] and "| primary-db |" in body["body"] and "demo-storm-1" in body["body"]
    # the branch landed on origin, authored by Faultline, with every service in the diff
    author = _git("log", "-1", "--format=%an", "faultline/demo-storm-1-bounded-retries", cwd=bare)
    assert author == "Faultline"
    changed = _git("diff", "--name-only", "main", "faultline/demo-storm-1-bounded-retries", cwd=bare).splitlines()
    for svc in ("gateway", "api", "cache", "inventory", "payments", "primary-db"):
        assert any(f"demo/shop/{svc}/" in path for path in changed), svc
    assert "sandbox/services/orders/app.py" in changed
    assert not (tmp_path / "wt" / "pr-demo-storm-1").exists()  # worktree cleaned up
    assert "ghp_test" not in "".join(_git("config", "--list", cwd=work))


def test_existing_pull_request_is_reused(tmp_path):
    work, _ = _repo_with_remote(tmp_path)
    bundle = load_fixture("storm")
    http = Script([
        (422, {"message": "Validation Failed"}),
        (200, [{"html_url": "https://github.com/Jay-Thpr/HackMIT/pull/7", "number": 7}]),
    ])
    adapter = GitHubPatchAdapter("ghp_test", REPO, _fallback(), repo_root=work, workdir=tmp_path / "wt", http=http)
    patch = adapter.propose("again", bundle.verdict, bundle.triage)
    assert patch.reference.endswith("/pull/7")
    assert http.calls[1][0] == "GET" and "head=Jay-Thpr:faultline/again-bounded-retries" in http.calls[1][1]


def test_github_failure_falls_back_without_raising(tmp_path):
    work, _ = _repo_with_remote(tmp_path)
    bundle = load_fixture("storm")
    http = Script([(403, {"message": "Resource not accessible by personal access token"})])
    adapter = GitHubPatchAdapter("ghp_test", REPO, _fallback(), repo_root=work, workdir=tmp_path / "wt", http=http)
    patch = adapter.propose("denied", bundle.verdict, bundle.triage)
    assert patch.provider == "fallback" and patch.reference == "branch:faultline/fallback-retry-cap"
    assert "403" in patch.summary


def test_disabled_without_token(tmp_path):
    adapter = GitHubPatchAdapter(None, REPO, _fallback(), repo_root=tmp_path)
    bundle = load_fixture("storm")
    assert not adapter.enabled
    assert adapter.propose("x", bundle.verdict, bundle.triage) == _fallback()
    assert adapter.revise("x", _fallback(), "evidence") is None


def test_body_lists_diagnosis_and_rejected_hypotheses():
    bundle = load_fixture("storm")
    body = pull_request_body("inc-1", bundle.verdict, bundle.triage)
    assert f"`{bundle.verdict.diagnosis}`" in body
    for h in bundle.triage.hypotheses:
        assert f"`{h.id}`" in body
    assert body.count("| after_release |") + body.count("| during |") == len(
        {(o.phase.value, o.metric) for o in bundle.verdict.observations}
    )


class _Static:
    def __init__(self, proposal, revised=None):
        self.proposal, self.revised, self.calls = proposal, revised, []

    def propose(self, *a):
        self.calls.append("propose")
        return self.proposal

    def revise(self, *a):
        self.calls.append("revise")
        return self.revised


def test_chain_uses_github_only_when_primary_falls_back():
    bundle = load_fixture("storm")
    devin = PatchProposal("devin", "https://github.com/Jay-Thpr/HackMIT/pull/3", "Devin session", session_id="s")
    github = PatchProposal("github", "https://github.com/Jay-Thpr/HackMIT/pull/4", "PR #4")
    chain = PatchFallbackChain(_Static(devin), _Static(github))
    assert chain.propose("i", bundle.verdict, bundle.triage) == devin
    chain = PatchFallbackChain(_Static(_fallback()), _Static(github))
    out = chain.propose("i", bundle.verdict, bundle.triage)
    assert out.provider == "github" and out.reference == github.reference and "Prebuilt patch" in out.summary
    chain = PatchFallbackChain(_Static(_fallback()), _Static(_fallback()))
    assert chain.propose("i", bundle.verdict, bundle.triage) == _fallback()
    assert chain.revise("i", github, "e") is None
