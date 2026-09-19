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

    result = DevinAdapter("key", "repo", _fallback(), poll_s=0, sleep=lambda _: None, http=http).propose(
        "i", bundle.verdict, bundle.triage
    )
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

    result = DevinAdapter("key", "repo", _fallback(), http=http).propose("i", bundle.verdict, bundle.triage)
    assert result.provider == "fallback"
    assert "https://devin/session/1" in result.summary


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
