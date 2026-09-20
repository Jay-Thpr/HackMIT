import json

from faultline_product.adapters.devin import DevinAdapter, _pull_request_url
from faultline_product.fixtures import load_fixture
from faultline_product.ports import PatchProposal

ORG = "org-test"
SESSIONS = f"https://api.devin.ai/v3/organizations/{ORG}/sessions"


def _fallback():
    return PatchProposal("fallback", "branch:faultline/fallback-retry-cap", "Prebuilt patch")


def _adapter(http, **kw):
    kw.setdefault("poll_s", 0)
    kw.setdefault("sleep", lambda _: None)
    return DevinAdapter("cog_key", "github.com/Jay-Thpr/HackMIT", _fallback(), org_id=ORG, http=http, **kw)


def _session(status="running", detail="working", prs=None, structured=None, sid="abc123"):
    return {
        "session_id": sid,
        "url": f"https://app.devin.ai/sessions/{sid}",
        "status": status,
        "status_detail": detail,
        "pull_requests": prs or [],
        "structured_output": structured,
        "acus_consumed": 0.4,
    }


class Script:
    """Replays scripted responses and records every request."""

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, *, headers, data=None):
        self.calls.append((method, url, json.loads(data) if data else None, headers))
        item = self.responses.pop(0)
        return item if isinstance(item, tuple) else (200, item)


def test_disabled_without_key_or_org_returns_fallback():
    bundle = load_fixture("storm")
    assert DevinAdapter(None, "repo", _fallback(), org_id=ORG).propose("i", bundle.verdict, bundle.triage) == _fallback()
    assert DevinAdapter("cog_key", "repo", _fallback(), org_id=None).propose("i", bundle.verdict, bundle.triage) == _fallback()
    assert not DevinAdapter("cog_key", "repo", _fallback()).enabled


def test_propose_uses_v3_create_and_waits_until_devin_stops_working():
    bundle = load_fixture("storm")
    script = Script([
        _session(status="claimed", detail=None),
        _session(detail="working"),
        _session(detail="working", prs=[{"pr_url": "https://github.com/Jay-Thpr/HackMIT/pull/9", "pr_state": "open"}]),
        _session(detail="waiting_for_user", prs=[{"pr_url": "https://github.com/Jay-Thpr/HackMIT/pull/9", "pr_state": "open"}]),
    ])
    result = _adapter(script).propose("inc-1", bundle.verdict, bundle.triage)

    method, url, body, headers = script.calls[0]
    assert (method, url) == ("POST", SESSIONS)
    assert headers["Authorization"] == "Bearer cog_key"
    assert body["repos"] == ["Jay-Thpr/HackMIT"] and body["max_acu_limit"] == 5
    assert body["tags"] == ["faultline", "inc-1"] and "pr_url" in body["structured_output_schema"]["properties"]
    assert "H_meta" in body["prompt"] or "retry storm" in body["prompt"].lower()
    assert all(c[1] == f"{SESSIONS}/devin-abc123" for c in script.calls[1:])  # devin- prefix on reads
    assert result.provider == "devin" and result.session_id == "abc123" and result.revision == 0
    assert result.reference == "https://github.com/Jay-Thpr/HackMIT/pull/9"
    assert not script.responses  # kept polling while working even though the PR already existed


def test_structured_output_is_the_pr_source_when_pull_requests_lag():
    assert _pull_request_url(_session(structured={"pr_url": "https://github.com/x/y/pull/3", "summary": "s"})) == "https://github.com/x/y/pull/3"
    assert _pull_request_url(_session(prs=[{"pr_url": "https://github.com/x/y/pull/4", "pr_state": "open"}], structured={"pr_url": "old"})) == "https://github.com/x/y/pull/4"
    assert _pull_request_url(_session()) is None


def test_finished_without_pr_falls_back_and_names_the_session():
    bundle = load_fixture("storm")
    script = Script([_session(status="claimed", detail=None), _session(status="exit", detail="finished")])
    result = _adapter(script).propose("inc-2", bundle.verdict, bundle.triage)
    assert result.provider == "fallback" and result.reference == _fallback().reference
    assert "https://app.devin.ai/sessions/abc123" in result.summary


def test_api_error_falls_back_with_detail():
    bundle = load_fixture("storm")
    result = _adapter(Script([(403, {"detail": "Forbidden: legacy key"})])).propose("i", bundle.verdict, bundle.triage)
    assert result.provider == "fallback" and "403" in result.summary and "legacy" in result.summary


def test_timeout_returns_whatever_pr_exists():
    bundle = load_fixture("storm")
    forever = _session(detail="working", prs=[{"pr_url": "https://github.com/x/y/pull/1", "pr_state": "open"}])

    def http(method, url, *, headers, data=None):
        return (200, _session(status="claimed", detail=None)) if method == "POST" else (200, forever)

    result = _adapter(http, timeout_s=0).propose("i", bundle.verdict, bundle.triage)
    assert result.provider == "devin" and result.reference == "https://github.com/x/y/pull/1"


def test_revise_posts_to_messages_and_requires_devin_to_work_before_done_counts():
    pr = [{"pr_url": "https://github.com/x/y/pull/9", "pr_state": "open"}]
    script = Script([
        _session(detail="waiting_for_user", prs=pr),  # POST /messages returns the (still idle) session
        _session(detail="waiting_for_user", prs=pr),  # stale done state right after posting: must not count
        _session(detail="working", prs=pr),
        _session(detail="working", prs=pr),
        _session(detail="waiting_for_user", prs=pr),  # done after working: counts
    ])
    patch = PatchProposal("devin", "https://github.com/x/y/pull/9", "Devin session", session_id="abc123")
    revised = _adapter(script).revise("inc-1", patch, "clone SLO still breached 3/4 windows")

    method, url, body, _ = script.calls[0]
    assert (method, url) == ("POST", f"{SESSIONS}/devin-abc123/messages")
    assert "clone SLO still breached" in body["message"] and "revision 1" in body["message"]
    assert revised.revision == 1 and revised.session_id == "abc123" and revised.reference == patch.reference
    assert not script.responses


def test_revise_returns_none_when_not_revisable_or_rejected():
    patch_no_session = _fallback()
    assert _adapter(Script([])).revise("i", patch_no_session, "e") is None
    patch = PatchProposal("devin", "pr", "s", session_id="abc123")
    assert _adapter(Script([(409, {"detail": "session terminated"})])).revise("i", patch, "e") is None
    assert DevinAdapter(None, "repo", _fallback(), org_id=ORG).revise("i", patch, "e") is None


def test_repo_slug_normalisation():
    for repo in ("github.com/Jay-Thpr/HackMIT", "https://github.com/Jay-Thpr/HackMIT.git", "Jay-Thpr/HackMIT"):
        assert DevinAdapter("k", repo, _fallback(), org_id=ORG)._repo_slug() == "Jay-Thpr/HackMIT"
