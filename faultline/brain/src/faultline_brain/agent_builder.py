from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from .elastic_investigation import (
    EVIDENCE_CONTEXT_INSTRUCTIONS,
    INFERENCE_ID,
    INVESTIGATOR_AGENT_INSTRUCTIONS,
    REPORT_AGENT_INSTRUCTIONS,
    ROLE_AGENT_IDS,
    STRUCTURED_OUTPUT_INSTRUCTIONS,
    TRIAGE_AGENT_INSTRUCTIONS,
)

_ROLE_INSTRUCTIONS = {
    "triage": TRIAGE_AGENT_INSTRUCTIONS,
    "investigator": INVESTIGATOR_AGENT_INSTRUCTIONS,
    "report": REPORT_AGENT_INSTRUCTIONS,
}

_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_MESSAGE_CHARS = 256 * 1024
_MAX_CONVERSATION_ID_CHARS = 256


class AgentBuilderError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validated_origin(kibana_url: str) -> str:
    if not isinstance(kibana_url, str) or not kibana_url.strip():
        raise AgentBuilderError("a Kibana URL is required")
    try:
        parts = urllib.parse.urlsplit(kibana_url.strip())
        port = parts.port
    except ValueError:
        raise AgentBuilderError("invalid Kibana URL") from None
    if parts.scheme != "https" or not parts.hostname:
        raise AgentBuilderError("Kibana URL must be an https origin")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise AgentBuilderError("Kibana URL must not contain credentials, query or fragment")
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in kibana_url.strip()):
        raise AgentBuilderError("invalid Kibana URL")
    path = parts.path.rstrip("/")
    if path and re.fullmatch(r"/s/[A-Za-z0-9_-]+", path) is None:
        raise AgentBuilderError("Kibana URL path must be empty or /s/<space-id>")
    try:
        host = parts.hostname.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        raise AgentBuilderError("invalid Kibana URL") from None
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return f"https://{netloc}{path}"


class AgentBuilderClient:
    def __init__(
        self,
        kibana_url: str,
        api_key: str,
        *,
        role: str,
        inference_id: str = INFERENCE_ID,
        timeout_s: float = 30,
        request: Callable[[dict], dict] | None = None,
        provenance_sink: Callable[[dict], None] | None = None,
        context: dict | None = None,
    ):
        if role not in ROLE_AGENT_IDS:
            raise AgentBuilderError("unknown Agent Builder proposal role")
        if not isinstance(api_key, str) or not api_key or api_key != api_key.strip() or "\n" in api_key or "\r" in api_key:
            raise AgentBuilderError("an Agent Builder API key is required")
        if not isinstance(inference_id, str) or not _SAFE_IDENTIFIER.fullmatch(inference_id):
            raise AgentBuilderError("invalid Agent Builder inference id")
        if not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise AgentBuilderError("timeout_s must be finite and positive")
        self._origin = _validated_origin(kibana_url)
        self._api_key = api_key
        self._role = role
        self._agent_id = ROLE_AGENT_IDS[role]
        self._inference_id = inference_id
        self._timeout_s = float(timeout_s)
        self._request = request or self._post
        self._provenance_sink = provenance_sink
        if context is None:
            self._context = None
        else:
            try:
                self._context = json.loads(json.dumps(context, allow_nan=False))
            except (TypeError, ValueError):
                raise AgentBuilderError("context is not JSON serializable") from None
        self.chat = SimpleNamespace(completions=self)

    @classmethod
    def from_env(cls, *, role: str, env=None, provenance_sink=None) -> AgentBuilderClient:
        source = os.environ if env is None else env
        return cls(
            source.get("KIBANA_URL"),
            source.get("ELASTIC_AGENT_BUILDER_API_KEY"),
            role=role,
            inference_id=source.get("FAULTLINE_AGENT_BUILDER_INFERENCE_ID") or INFERENCE_ID,
            provenance_sink=provenance_sink,
        )

    def with_context(self, context: dict, *, provenance_sink=None):
        return AgentBuilderClient(
            self._origin,
            self._api_key,
            role=self._role,
            inference_id=self._inference_id,
            timeout_s=self._timeout_s,
            request=self._request,
            provenance_sink=self._provenance_sink if provenance_sink is None else provenance_sink,
            context=context,
        )

    def create(self, *, model, messages, response_format):
        del model
        input_payload = {"messages": messages, "response_format": response_format}
        instructions = _ROLE_INSTRUCTIONS[self._role] + "\n" + STRUCTURED_OUTPUT_INSTRUCTIONS
        if self._context is not None:
            input_payload["context"] = self._context
            instructions += "\n" + EVIDENCE_CONTEXT_INSTRUCTIONS
        body = {
            "agent_id": self._agent_id,
            "inference_id": self._inference_id,
            "access_control": {"access_mode": "private"},
            "input": json.dumps(input_payload, allow_nan=False),
            "configuration_overrides": {
                "instructions": instructions,
                "tools": [],
                "skill_ids": [],
                "enable_elastic_capabilities": False,
            },
        }
        try:
            payload = self._request(body)
        except AgentBuilderError:
            raise
        except Exception as exc:
            raise AgentBuilderError(
                f"agent builder request failed: {type(exc).__name__}"
            ) from None
        message = self._message(payload)
        if self._provenance_sink is not None:
            self._provenance_sink(
                {
                    "provider": "agent_builder",
                    "role": self._role,
                    "agent_id": self._agent_id,
                    "inference_id": self._inference_id,
                    "conversation_id": self._conversation_id(payload),
                    "status": "response_received",
                }
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=message))],
            usage=None,
        )

    def _post(self, body: dict) -> dict:
        request = urllib.request.Request(
            self._origin + "/api/agent_builder/converse",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"ApiKey {self._api_key}",
                "kbn-xsrf": "true",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect
        )
        with opener.open(request, timeout=self._timeout_s) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise AgentBuilderError("agent builder response exceeded the size bound")
        try:
            return json.loads(raw)
        except ValueError:
            raise AgentBuilderError("agent builder response was not JSON") from None

    @staticmethod
    def _conversation_id(payload: dict) -> str | None:
        value = payload.get("conversation_id")
        if (
            isinstance(value, str)
            and len(value) <= _MAX_CONVERSATION_ID_CHARS
            and _SAFE_IDENTIFIER.fullmatch(value)
        ):
            return value
        return None

    @staticmethod
    def _message(payload: Any) -> str:
        if not isinstance(payload, dict):
            raise AgentBuilderError("agent builder payload was not an object")
        status = payload.get("status")
        if status is not None and status != "completed":
            raise AgentBuilderError("agent builder response did not complete")
        steps = payload.get("steps")
        if steps is not None:
            if not isinstance(steps, list) or any(not isinstance(s, dict) for s in steps):
                raise AgentBuilderError("agent builder response had malformed steps")
            if any(s.get("type") == "tool_call" for s in steps):
                raise AgentBuilderError("agent builder attempted a tool call with tools disabled")
        response = payload.get("response")
        if not isinstance(response, dict):
            raise AgentBuilderError("agent builder payload had no response object")
        message = response.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > _MAX_MESSAGE_CHARS:
            raise AgentBuilderError("agent builder response message was empty or oversized")
        return message


class FallbackInvestigatorAgent:
    def __init__(self, primary, fallback=None, *, provenance_sink=None):
        self._primary = primary
        self._fallback = fallback
        self._sink = provenance_sink

    def propose(self, *args, **kwargs):
        try:
            proposal = self._primary.propose(*args, **kwargs)
        except (RuntimeError, ValueError) as exc:
            self._emit("agent_builder", "rejected", type(exc).__name__)
            if self._fallback is None:
                raise
            try:
                proposal = self._fallback.propose(*args, **kwargs)
            except Exception as fallback_exc:
                self._emit("openai", "rejected", type(fallback_exc).__name__)
                raise RuntimeError(
                    f"fallback investigator proposal failed: {type(fallback_exc).__name__}"
                ) from None
            self._emit("openai", "validated")
            return proposal
        self._emit("agent_builder", "validated")
        return proposal

    def _emit(self, provider: str, status: str, reason: str | None = None) -> None:
        if self._sink is None:
            return
        event = {"provider": provider, "role": "investigator", "status": status}
        if reason is not None:
            event["reason"] = reason
        self._sink(event)
