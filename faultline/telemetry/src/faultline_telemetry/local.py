import json
import threading
from datetime import datetime
from pathlib import Path

from faultline_contracts import Fingerprint

SCHEMA = "faultline-local-c1/1"


class CorruptStoreError(ValueError):
    pass


class JsonlFingerprintStore:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()

    def write(self, fingerprint: Fingerprint, *, incident_id=None, clone_id=None) -> None:
        row = {
            "schema_version": SCHEMA,
            "incident_id": incident_id,
            "clone_id": clone_id,
            "fingerprint": fingerprint.model_dump(mode="json"),
        }
        line = json.dumps(row) + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def query(self, start: datetime, end: datetime, *, incident_id=None, clone_id=None):
        if not self._path.exists():
            return []
        with self._lock:
            raw = self._path.read_text(encoding="utf-8")
        if raw and not raw.endswith("\n"):
            raw = raw[: raw.rfind("\n") + 1]
        seen: dict[tuple, Fingerprint] = {}
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CorruptStoreError(f"corrupt row in {self._path}: {exc}") from exc
            if not isinstance(row, dict) or row.get("schema_version") != SCHEMA \
                    or not isinstance(row.get("fingerprint"), dict):
                raise CorruptStoreError(f"invalid row in {self._path}")
            if row.get("incident_id") != incident_id or row.get("clone_id") != clone_id:
                continue
            fp = Fingerprint(**row["fingerprint"])
            if fp.window_start < start or fp.window_end > end:
                continue
            key = (row.get("incident_id"), row.get("clone_id"),
                   fp.window_start.isoformat(), fp.window_end.isoformat())
            existing = seen.get(key)
            if existing is not None and existing != fp:
                raise CorruptStoreError(f"conflicting duplicate rows for window {key}")
            seen[key] = fp
        return sorted(seen.values(), key=lambda f: f.window_start)
