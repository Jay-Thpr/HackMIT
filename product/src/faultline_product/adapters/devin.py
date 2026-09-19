import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from faultline_contracts import TriageResult, Verdict

from ..ports import PatchProposal

API = "https://api.devin.ai/v1"
DONE = {"finished", "expired", "blocked"}  # Devin has stopped working on the last message


def _http_request(
    method: str, url: str, *, headers: dict[str, str], data: bytes | None = None
) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


class DevinAdapter:
    def __init__(
        self,
        api_key: str | None,
        repo: str,
        fallback: PatchProposal,
        timeout_s: int = 600,
        poll_s: float = 10,
        sleep: Callable[[float], None] = time.sleep,
        http: Callable[..., Any] = _http_request,
    ):
        self._api_key = api_key
        self._repo = repo
        self._fallback = fallback
        self._timeout_s = timeout_s
        self._poll_s = poll_s
        self._sleep = sleep
        self._http = http

    def propose(self, incident_id: str, verdict: Verdict, triage: TriageResult) -> PatchProposal:
        if self._api_key is None:
            return self._fallback
        try:
            status, created = self._call(
                "POST",
                f"{API}/sessions",
                self._headers(),
                {
                    "prompt": self._prompt(incident_id, verdict, triage),
                    "title": f"Faultline fix for {incident_id}",
                    "tags": ["faultline", incident_id],
                },
            )
            if status >= 400:
                return self._api_error(status)
            session_id = created["session_id"]
            session_url = created.get("url", f"https://app.devin.ai/sessions/{session_id}")
            pull_request = self._await_pull_request(session_id, self._timeout_s)
            if pull_request is None:
                return self._fallback_with_session(session_url)
            return PatchProposal(
                "devin", pull_request, f"Devin session {session_id}", session_id=session_id
            )
        except Exception:  # noqa: BLE001
            return self._fallback_with_summary("devin api error: request failed")

    def revise(
        self, incident_id: str, patch: PatchProposal, evidence: str
    ) -> PatchProposal | None:
        """Send measured evidence back into the same session; return the revised patch once
        Devin has pushed again (same PR, new commits) or None if it cannot be revised."""
        if self._api_key is None or patch.session_id is None:
            return None
        revision = patch.revision + 1
        try:
            status, _ = self._call(
                "POST",
                f"{API}/sessions/{patch.session_id}/message",
                self._headers(),
                {"message": self._revision_prompt(incident_id, patch, evidence, revision)},
            )
            if status >= 400:
                return None
            self._sleep(self._poll_s)  # let the session leave its terminal state
            pull_request = self._await_pull_request(patch.session_id, self._timeout_s)
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

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def _await_pull_request(self, session_id: str, timeout_s: float) -> str | None:
        """Poll the session until it is done working; return the PR url if it has one."""
        deadline = time.monotonic() + timeout_s
        while True:
            status, session = self._call("GET", f"{API}/sessions/{session_id}", self._headers(), None)
            if status >= 400:
                return None
            pull_request = (session.get("pull_request") or {}).get("url")
            done = session.get("status_enum") in DONE
            if done or time.monotonic() >= deadline:
                return pull_request
            self._sleep(self._poll_s)

    def _call(
        self, method: str, url: str, headers: dict[str, str], payload: dict | None
    ) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        result = self._http(method, url, headers=headers, data=data)
        if isinstance(result, tuple) and len(result) == 2:
            return int(result[0]), result[1]
        return 200, result

    def _prompt(self, incident_id: str, verdict: Verdict, triage: TriageResult) -> str:
        hypothesis = next(
            (item for item in triage.hypotheses if item.id == verdict.diagnosis), None
        )
        label = hypothesis.label if hypothesis else verdict.diagnosis
        description = hypothesis.description if hypothesis else ""
        observations = "\n".join(
            f"- {item.metric} ({item.phase.value}): z={item.z:g}" for item in verdict.observations
        )
        return (
            f"Repository: {self._repo}\nIncident: {incident_id}\n"
            f"Diagnosis hypothesis: {label} — {description}\nVerdict: {verdict.summary}\n"
            f"Observations:\n{observations}\n\n"
            f"Open a PR in {self._repo} that makes Orders' retry policy bounded "
            "(cap retries, exponential backoff with jitter); keep the runtime retry "
            "override endpoint intact; don't touch anything else."
        )

    def _revision_prompt(
        self, incident_id: str, patch: PatchProposal, evidence: str, revision: int
    ) -> str:
        return (
            f"Faultline verified your patch for incident {incident_id} ({patch.reference}) and it "
            f"did not hold up. Measured evidence:\n{evidence}\n\n"
            f"Please revise on the same branch/PR (revision {revision}): keep retries bounded with "
            "exponential backoff and jitter, keep the runtime retry override endpoint intact, and "
            "push when done. Faultline will replay the incident against the new commit."
        )

    def _api_error(self, status: int) -> PatchProposal:
        return self._fallback_with_summary(f"devin api error: {status}")

    def _fallback_with_session(self, session_url: str) -> PatchProposal:
        return self._fallback_with_summary(
            f"{self._fallback.summary}; Devin session: {session_url}"
        )

    def _fallback_with_summary(self, summary: str) -> PatchProposal:
        return PatchProposal("fallback", self._fallback.reference, summary)


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
