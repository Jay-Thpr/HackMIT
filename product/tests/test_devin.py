from faultline_product.adapters.devin import DevinAdapter
from faultline_product.fixtures import load_fixture
from faultline_product.ports import PatchProposal


def _fallback():
    return PatchProposal("fallback", "branch:faultline/fallback-retry-cap", "Prebuilt patch")


def test_no_key_returns_fallback():
    bundle = load_fixture("storm")
    result = DevinAdapter(None, "repo", _fallback()).propose("i", bundle.verdict, bundle.triage)
    assert result == _fallback()


def test_pr_appears_on_second_poll():
    bundle = load_fixture("storm")
    responses = [
        {"session_id": "devin-1", "url": "https://devin/session/1"},
        {"status_enum": "working", "pull_request": None},
        {"status_enum": "finished", "pull_request": {"url": "https://github/pr/1"}},
    ]

    def http(method, url, **kwargs):
        del method, url, kwargs
        return responses.pop(0)

    result = DevinAdapter(
        "key", "repo", _fallback(), poll_s=0, sleep=lambda _: None, http=http
    ).propose("i", bundle.verdict, bundle.triage)
    assert result.provider == "devin"
    assert result.reference == "https://github/pr/1"


def test_finished_without_pr_mentions_session_url():
    bundle = load_fixture("storm")
    responses = [
        {"session_id": "devin-1", "url": "https://devin/session/1"},
        {"status_enum": "finished", "pull_request": None},
    ]

    def http(method, url, **kwargs):
        del method, url, kwargs
        return responses.pop(0)

    result = DevinAdapter("key", "repo", _fallback(), http=http).propose(
        "i", bundle.verdict, bundle.triage
    )
    assert result.provider == "fallback"
    assert "https://devin/session/1" in result.summary


def test_proposal_carries_session_id_and_waits_for_devin_to_finish():
    bundle = load_fixture("storm")
    responses = [
        {"session_id": "devin-1", "url": "https://devin/session/1"},
        {"status_enum": "working", "pull_request": {"url": "https://github/pr/1"}},  # PR open, still pushing
        {"status_enum": "blocked", "pull_request": {"url": "https://github/pr/1"}},
    ]
    result = DevinAdapter(
        "key", "repo", _fallback(), poll_s=0, sleep=lambda _: None, http=lambda m, u, **k: responses.pop(0)
    ).propose("i", bundle.verdict, bundle.triage)
    assert result.session_id == "devin-1" and result.revision == 0
    assert not responses  # waited until Devin stopped working before handing the PR to the canary


def test_revise_posts_evidence_to_session_and_returns_next_revision():
    calls = []
    responses = [
        None,  # POST message -> null on success
        {"status_enum": "working", "pull_request": {"url": "https://github/pr/1"}},
        {"status_enum": "finished", "pull_request": {"url": "https://github/pr/1"}},
    ]

    def http(method, url, *, headers, data=None):
        calls.append((method, url, data))
        return responses.pop(0)

    patch = PatchProposal("devin", "https://github/pr/1", "Devin session devin-1", session_id="devin-1")
    revised = DevinAdapter("key", "repo", _fallback(), poll_s=0, sleep=lambda _: None, http=http).revise(
        "inc-1", patch, "clone SLO still breached 3/4 windows"
    )

    assert calls[0][0:2] == ("POST", "https://api.devin.ai/v1/sessions/devin-1/message")
    assert b"clone SLO still breached" in calls[0][2] and b"revision 1" in calls[0][2]
    assert revised.revision == 1 and revised.session_id == "devin-1"
    assert revised.reference == "https://github/pr/1"


def test_revise_returns_none_without_session_or_key():
    adapter = DevinAdapter("key", "repo", _fallback(), http=lambda *a, **k: (500, {}))
    assert adapter.revise("i", _fallback(), "evidence") is None  # fallback patch has no session
    assert DevinAdapter(None, "repo", _fallback()).revise(
        "i", PatchProposal("devin", "pr", "s", session_id="devin-1"), "evidence"
    ) is None


def test_timeout_returns_fallback():
    bundle = load_fixture("storm")
    calls = []

    def http(method, url, **kwargs):
        del kwargs
        calls.append((method, url))
        if method == "POST":
            return {"session_id": "devin-1", "url": "https://devin/session/1"}
        return {"status_enum": "working", "pull_request": None}

    result = DevinAdapter("key", "repo", _fallback(), timeout_s=0, http=http).propose(
        "i", bundle.verdict, bundle.triage
    )
    assert result.provider == "fallback"
    assert calls
