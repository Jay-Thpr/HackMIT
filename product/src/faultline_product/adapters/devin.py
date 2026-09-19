"""Devin code adapter (Devin API v3, service-user keys).

Measured 2026-09-19 with a ``cog_`` service-user key: ``/v1`` returns 403, ``/v3`` works.
  POST /v3/organizations/{org}/sessions                      -> SessionResponse
  GET  /v3/organizations/{org}/sessions/devin-{id}           -> SessionResponse
  POST /v3/organizations/{org}/sessions/devin-{id}/messages  -> SessionResponse
SessionResponse: status in {new, claimed, running, exit, error, suspended, resuming};
status_detail when running in {working, waiting_for_user, waiting_for_approval, finished};
pull_requests: [{pr_url, pr_state}]; structured_output: validated JSON we ask for.
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from faultline_contracts import TriageResult, Verdict

from ..ports import PatchProposal

API = "https://api.devin.ai/v3"
WORKING = {"working"}
# Devin has stopped acting on the last message (it answered, finished, or needs a human).
DONE_DETAIL = {"waiting_for_user", "waiting_for_approval", "finished"}
DONE_STATUS = {"exit", "error", "suspended"}
STRUCTURED_OUTPUT = {
    "type": "object",
    "properties": {
        "pr_url": {"type": "string", "description": "URL of the pull request with the fix"},
        "summary": {"type": "string", "description": "one paragraph: what changed and why"},
    },
    "required": ["pr_url", "summary"],
}


def _http_request(
    method: str, url: str, *, headers: dict[str, str], data: bytes | None = None
) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            return error.code, json.loads(body) if body else {}
        except json.JSONDecodeError:
            return error.code, {"detail": body.decode(errors="replace")}


def _devin_id(session_id: str) -> str:
    return session_id if session_id.startswith("devin-") else f"devin-{session_id}"


class DevinAdapter:
    def __init__(
        self,
        api_key: str | None,
        repo: str,
        fallback: PatchProposal,
        org_id: str | None = None,
        timeout_s: int = 900,
        poll_s: float = 10,
        max_acu_limit: int | None = 5,
        sleep: Callable[[float], None] = time.sleep,
        http: Callable[..., Any] = _http_request,
    ):
        self._api_key = api_key
        self._org_id = org_id
        self._repo = repo
        self._fallback = fallback
        self._timeout_s = timeout_s
        self._poll_s = poll_s
        self._max_acu_limit = max_acu_limit
        self._sleep = sleep
        self._http = http

    @property
    def enabled(self) -> bool:
        return bool(self._api_key and self._org_id)

    def propose(self, incident_id: str, verdict: Verdict, triage: TriageResult) -> PatchProposal:
        if not self.enabled:
            return self._fallback
        try:
            status, created = self._call(
                "POST",
                self._url("sessions"),
                {
                    "prompt": self._prompt(incident_id, verdict, triage),
                    "title": f"Faultline fix for {incident_id}",
                    "tags": ["faultline", incident_id],
                    "repos": [self._repo_slug()],
                    "max_acu_limit": self._max_acu_limit,
                    "structured_output_schema": STRUCTURED_OUTPUT,
                    "structured_output_required": True,
                },
            )
            if status >= 400:
                return self._api_error(status, created)
            session_id = created["session_id"]
            session_url = created.get("url", f"https://app.devin.ai/sessions/{session_id}")
            pull_request = self._await_pull_request(session_id, require_work=False)
            if pull_request is None:
                return self._fallback_with_session(session_url)
            return PatchProposal(
                "devin", pull_request, f"Devin session {session_url}", session_id=session_id
            )
        except Exception as exc:  # noqa: BLE001 - never let the code stage take the loop down
            return self._fallback_with_summary(f"devin api error: {type(exc).__name__}: {exc}")

    def revise(
        self, incident_id: str, patch: PatchProposal, evidence: str
    ) -> PatchProposal | None:
        """Send measured evidence back into the same session; return the revised patch once
        Devin has worked and stopped again (same PR, new commits), or None if not revisable."""
        if not self.enabled or patch.session_id is None:
            return None
        revision = patch.revision + 1
        try:
            status, _ = self._call(
                "POST",
                self._url(f"sessions/{_devin_id(patch.session_id)}/messages"),
                {"message": self._revision_prompt(incident_id, patch, evidence, revision)},
            )
            if status >= 400:
                return None
            pull_request = self._await_pull_request(patch.session_id, require_work=True)
        except Exception:  # noqa: BLE001
            return None
        if pull_request is None:
            return None
        return PatchProposal(
            "devin",
            pull_request,
            f"Devin revision {revision} of session {patch.session_id}",
            session_id=patch.session_id,
            revision=revision,
        )

    # -- polling -----------------------------------------------------------------------------

    def _await_pull_request(self, session_id: str, *, require_work: bool) -> str | None:
        """Poll until Devin has stopped acting on the last message; return the PR url if any.

        ``require_work`` (revisions): the session is still in its previous done state right
        after we post, so we must see it working once before a done state counts. If it never
        picks the message up within the grace period, the old done state is accepted.
        """
        deadline = time.monotonic() + self._timeout_s
        grace_until = time.monotonic() + max(6 * self._poll_s, 30)
        worked = not require_work
        while True:
            status, session = self._call("GET", self._url(f"sessions/{_devin_id(session_id)}"), None)
            if status >= 400:
                return None
            detail = session.get("status_detail")
            if detail in WORKING:
                worked = True
            done = session.get("status") in DONE_STATUS or (
                session.get("status") == "running" and detail in DONE_DETAIL
            )
            timed_out = time.monotonic() >= deadline
            if timed_out or (done and (worked or time.monotonic() >= grace_until)):
                return _pull_request_url(session)
            self._sleep(self._poll_s)

    # -- http --------------------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{API}/organizations/{self._org_id}/{path}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def _call(self, method: str, url: str, payload: dict | None) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        result = self._http(method, url, headers=self._headers(), data=data)
        if isinstance(result, tuple) and len(result) == 2:
            return int(result[0]), result[1] or {}
        return 200, result or {}

    def _repo_slug(self) -> str:
        slug = self._repo
        for prefix in ("https://", "http://", "github.com/"):
            if slug.startswith(prefix):
                slug = slug[len(prefix):]
        return slug.removesuffix(".git").strip("/")

    # -- prompts -----------------------------------------------------------------------------

    def _prompt(self, incident_id: str, verdict: Verdict, triage: TriageResult) -> str:
        hypothesis = next(
            (item for item in triage.hypotheses if item.id == verdict.diagnosis), None
        )
        label = hypothesis.label if hypothesis else verdict.diagnosis
        description = hypothesis.description if hypothesis else ""
        observations = "\n".join(
            f"- {item.metric} ({item.phase.value}): baseline {item.baseline:g} -> measured "
            f"{item.measured:g}, z={item.z:g} ({item.direction.value})"
            for item in verdict.observations
        )
        return (
            f"You are the durable-fix step of Faultline, an autonomous incident responder.\n"
            f"Repository: {self._repo}\nIncident: {incident_id}\n"
            f"Diagnosis (confirmed by a live production experiment): {label} — {description}\n"
            f"Verdict: {verdict.summary}\nMeasured observations:\n{observations}\n\n"
            "Task: open a pull request that makes Orders' retry policy bounded so a transient "
            "Payments/DB slowdown can no longer turn into a self-sustaining retry storm: cap "
            "retries, exponential backoff with full jitter, and a retry budget. The file is "
            "sandbox/services/orders/app.py. Keep the runtime retry override endpoint "
            "(/internal/retry_override), /stats and /healthz behaviour intact; do not change "
            "any other service. Branch from main, do not merge. When the PR is open, provide "
            "the structured output with pr_url and summary. Faultline will build your branch as "
            "orders-v2, replay the incident against it in a clean clone, then canary it at 5%."
        )

    def _revision_prompt(
        self, incident_id: str, patch: PatchProposal, evidence: str, revision: int
    ) -> str:
        return (
            f"Faultline verified your patch for incident {incident_id} ({patch.reference}) and it "
            f"did not hold up. Measured evidence:\n{evidence}\n\n"
            f"Please revise on the same branch/PR (revision {revision}): keep retries bounded with "
            "exponential backoff and jitter, keep the runtime retry override endpoint intact, push "
            "when done, and provide the structured output again. Faultline will replay the "
            "incident against the new commit."
        )

    # -- fallbacks ---------------------------------------------------------------------------

    def _api_error(self, status: int, body: dict) -> PatchProposal:
        detail = body.get("detail") or body.get("title") or ""
        return self._fallback_with_summary(f"devin api error: {status} {detail}".strip())

    def _fallback_with_session(self, session_url: str) -> PatchProposal:
        return self._fallback_with_summary(
            f"{self._fallback.summary}; Devin session without a PR: {session_url}"
        )

    def _fallback_with_summary(self, summary: str) -> PatchProposal:
        return PatchProposal("fallback", self._fallback.reference, summary)


def _pull_request_url(session: dict) -> str | None:
    for item in session.get("pull_requests") or []:
        if isinstance(item, dict) and item.get("pr_url"):
            return item["pr_url"]
    structured = session.get("structured_output") or {}
    if isinstance(structured, dict) and structured.get("pr_url"):
        return structured["pr_url"]
    return None


class FixtureDevinAdapter:
    def propose(self, incident_id: str, verdict: Verdict, triage: TriageResult) -> PatchProposal:
        del triage
        return PatchProposal(
            provider="devin",
            reference=f"devin://task/{incident_id}",
            summary=f"Add bounded exponential backoff and jitter for {verdict.diagnosis}",
            session_id=f"fixture-{incident_id}",
        )

    def revise(
        self, incident_id: str, patch: PatchProposal, evidence: str
    ) -> PatchProposal | None:
        del evidence
        revision = patch.revision + 1
        return PatchProposal(
            provider="devin",
            reference=f"devin://task/{incident_id}/rev{revision}",
            summary=f"Revision {revision} after measured evidence",
            session_id=patch.session_id,
            revision=revision,
        )
