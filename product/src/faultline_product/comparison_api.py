from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

SCHEMA_VERSION = "faultline-comparison/1"
ID_PATTERN = re.compile(r"cmp-[A-Za-z0-9_-]{1,100}")
MAX_FILES = 100
MAX_BYTES = 20 * 1024 * 1024


def _reject_constant(value):
    raise ValueError("nonfinite JSON")


def _root(directory: Path) -> Path | None:
    try:
        root = directory.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return root if root.is_dir() else None


def _load(path: Path, root: Path) -> dict[str, Any]:
    if path.is_symlink() or path.resolve().parent != root:
        raise HTTPException(404, "unknown comparison recording")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise HTTPException(404, "unknown comparison recording")
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized")
        data = json.loads(raw, parse_constant=_reject_constant)
        if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION
                or data.get("id") != path.stem or not ID_PATTERN.fullmatch(path.stem)
                or not isinstance(data.get("cases"), list) or not isinstance(data.get("runs"), list)
                or not isinstance(data.get("title"), str) or not isinstance(data.get("created_at"), str)):
            raise ValueError("invalid envelope")
        return data
    except OSError:
        raise HTTPException(404, "unknown comparison recording") from None
    except (ValueError, UnicodeError):
        raise HTTPException(422, "invalid comparison recording") from None


def _recordings(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    candidates: list[tuple[float, Path]] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for path in entries:
        try:
            if not path.match("cmp-*.json"):
                continue
            info = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(info.st_mode):
                continue
            candidates.append((info.st_mtime, path))
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)
    candidates = candidates[:MAX_FILES]
    out: list[tuple[Path, dict[str, Any]]] = []
    for _, path in candidates:
        try:
            out.append((path, _load(path, root)))
        except HTTPException:
            continue
    return out


def add_comparison_routes(app: FastAPI, directory: Path) -> None:
    @app.get("/api/comparisons")
    def comparisons() -> list[dict[str, Any]]:
        root = _root(directory)
        if root is None:
            return []
        return [
            {
                "id": data["id"],
                "title": data.get("title"),
                "created_at": data.get("created_at"),
                "run_count": len(data["runs"]),
            }
            for _, data in _recordings(root)
        ]

    @app.get("/api/comparisons/{recording_id}")
    def comparison(recording_id: str) -> dict[str, Any]:
        if not ID_PATTERN.fullmatch(recording_id):
            raise HTTPException(404, "unknown comparison recording")
        root = _root(directory)
        if root is None:
            raise HTTPException(404, "unknown comparison recording")
        return _load(root / f"{recording_id}.json", root)
