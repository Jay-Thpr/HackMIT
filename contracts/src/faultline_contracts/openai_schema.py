"""OpenAI structured-output helper for C2.

    resp = client.chat.completions.create(model=..., messages=..., response_format=triage_response_format())
    draft = TriageDraft.model_validate_json(resp.choices[0].message.content)
"""

from typing import Any

from .triage import TriageDraft

_STRIP = {"title", "default"}


def strictify(schema: Any) -> Any:
    """Make a pydantic JSON schema compatible with OpenAI strict mode: every object closed
    (additionalProperties false) with all properties required; titles/defaults removed."""
    if isinstance(schema, list):
        return [strictify(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k in _STRIP:
            continue
        if k in ("properties", "$defs"):
            out[k] = {name: strictify(sub) for name, sub in v.items()}  # keys are names, not keywords
        else:
            out[k] = strictify(v)
    if out.get("type") == "object":
        if "properties" not in out:
            raise ValueError(f"free-form object not allowed in strict schema: {schema}")
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


def strict_response_format(model: Any, name: str) -> dict[str, Any]:
    """OpenAI strict ``response_format`` for any pydantic model."""
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": strictify(model.model_json_schema())},
    }


def triage_response_format() -> dict[str, Any]:
    return strict_response_format(TriageDraft, "triage")
