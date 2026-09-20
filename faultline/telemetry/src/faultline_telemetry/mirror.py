import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .elasticsearch import document_id

log = logging.getLogger(__name__)


class MirroredElasticsearchClient:
    def __init__(self, primary, secondary, outbox: str | Path | None = None, *, retry_s: float = 1.0,
                 max_pending: int = 10000, max_bytes: int = 64 * 1024 * 1024, start: bool = True):
        if isinstance(secondary, (list, tuple)):
            if len(secondary) != 1:
                raise ValueError("exactly one durable display mirror is supported")
            secondary = secondary[0]
        if outbox is None:
            if not isinstance(getattr(secondary, "base_url", None), str):
                raise ValueError("an explicit outbox path is required for a custom mirror client")
            directory = os.environ.get("FAULTLINE_MIRROR_OUTBOX_DIR") or Path.home() / ".local/state/faultline"
            outbox = mirror_outbox_path(secondary.base_url, directory)
        if retry_s <= 0 or max_pending < 1 or max_bytes < 1:
            raise ValueError("mirror retry interval and outbox bounds must be positive")
        self.primary, self.secondary = primary, secondary
        self.mirrors = [secondary]
        self._retry_s, self._max_pending, self._max_bytes = retry_s, max_pending, max_bytes
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._closed = False
        self._released = False
        self._failures = self._rejected = 0
        self._last_error = None
        path = Path(outbox).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self._db = sqlite3.connect(path, timeout=0.1, check_same_thread=False)
        try:
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("CREATE TABLE IF NOT EXISTS outbox "
                             "(id TEXT PRIMARY KEY, idx TEXT NOT NULL, body TEXT NOT NULL, "
                             "created REAL NOT NULL, bytes INTEGER NOT NULL)")
            self._db.commit()
            self._thread = threading.Thread(target=self._run, name="elastic-display-mirror", daemon=True)
            if start:
                self._thread.start()
        except Exception:
            self._db.close()
            raise

    def index(self, *, index: str, document: dict[str, Any]) -> Any:
        result = self.primary.index(index=index, document=document)
        try:
            body = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
            size = len(body.encode())
            key = document_id(index, document)
            with self._lock, self._db:
                if self._closed:
                    raise RuntimeError("closed")
                self._db.execute("BEGIN IMMEDIATE")
                count, used = self._db.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM outbox").fetchone()
                old = self._db.execute("SELECT bytes FROM outbox WHERE id=?", (key,)).fetchone()
                if (not old and count >= self._max_pending) or used - (old[0] if old else 0) + size > self._max_bytes:
                    raise OverflowError("full")
                self._db.execute("INSERT INTO outbox VALUES (?, ?, ?, ?, ?) "
                                 "ON CONFLICT(id) DO UPDATE SET body=excluded.body, bytes=excluded.bytes",
                                 (key, index, body, time.time(), size))
            self._wake.set()
        except Exception as exc:
            with self._lock:
                self._rejected += 1
            self._failure(exc, "outbox enqueue rejected; evidence remains in primary")
        return result

    def search(self, **kwargs):
        return self.primary.search(**kwargs)

    def esql(self, *args, **kwargs):
        return self.primary.esql(*args, **kwargs)

    def refresh(self, index):
        return self.primary.refresh(index)

    def put_index_template(self, name, body):
        return self.primary.put_index_template(name, body)

    def _failure(self, exc, message):
        with self._lock:
            self._failures += 1
            self._last_error = type(exc).__name__
            report = self._failures == 1 or self._failures % 60 == 0
        if report:
            log.warning("Elasticsearch display mirror: %s (%s)", message, type(exc).__name__)

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    with self._lock:
                        row = self._db.execute("SELECT id, idx, body FROM outbox ORDER BY created LIMIT 1").fetchone()
                    if row is None:
                        self._wake.wait(self._retry_s)
                        self._wake.clear()
                        continue
                    key, index, body = row
                    self.secondary.index(index=index, document=json.loads(body))
                    with self._lock, self._db:
                        self._db.execute("DELETE FROM outbox WHERE id=? AND body=?", (key, body))
                        recovered = self._last_error is not None
                        self._last_error = None
                    if recovered:
                        log.info("Elasticsearch display mirror delivery recovered")
                except Exception as exc:
                    self._failure(exc, "delivery failed; persisted evidence will retry")
                    self._stop.wait(self._retry_s)
        finally:
            if self._closed:
                self._release()
            else:
                self._failure(RuntimeError(), "worker stopped; outbox retained for restart")

    def health(self) -> dict[str, Any]:
        with self._lock:
            count, oldest, size = self._db.execute(
                "SELECT COUNT(*), MIN(created), COALESCE(SUM(bytes), 0) FROM outbox"
            ).fetchone()
            return {"pending": count, "pending_bytes": size,
                    "oldest_pending_s": max(0, time.time() - oldest) if oldest else 0,
                    "failures": self._failures, "rejected": self._rejected,
                    "last_error": self._last_error, "worker_alive": self._thread.is_alive()}

    def _release(self):
        with self._lock:
            if self._released:
                return
            self._released = True
            try:
                self._db.close()
            finally:
                close = getattr(self.secondary, "close", None)
                if close:
                    close()

    def close(self, timeout_s: float = 12.0) -> None:
        with self._lock:
            if self._closed:
                return
            health = self.health()
            self._closed = True
        if health["pending"]:
            log.warning("Elasticsearch display mirror closing with %d persisted pending documents", health["pending"])
        self._stop.set()
        self._wake.set()
        try:
            if self._thread.ident is not None:
                self._thread.join(timeout_s)
            if self._thread.is_alive():
                log.warning("Elasticsearch display mirror still finishing delivery; outbox retained")
            else:
                self._release()
        finally:
            close = getattr(self.primary, "close", None)
            if close:
                close()


def mirror_outbox_path(url: str, directory: str | Path) -> Path:
    destination = hashlib.sha256(url.rstrip("/").encode()).hexdigest()[:16]
    return Path(directory) / f"mirror-{destination}.sqlite"
