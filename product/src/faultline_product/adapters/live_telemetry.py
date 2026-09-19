import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from faultline_contracts import WINDOW_S, Fingerprint
from faultline_telemetry.fingerprint import fingerprint_from_stats

Snapshot = dict[str, dict[str, Any]]
log = logging.getLogger(__name__)


class FingerprintWriter(Protocol):
    """Track 2 persistence boundary; metadata remains outside the C1 payload."""

    def write(
        self, fingerprint: Fingerprint, *, incident_id: str | None = None, clone_id: str | None = None
    ) -> None: ...


class TelemetryUnavailable(RuntimeError):
    pass


def _utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def fingerprint_from_snapshots(
    prev: Snapshot,
    cur: Snapshot,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Fingerprint:
    """Owner 2's canonical C1 builder, plus the optional orders-v2 canary service."""
    start = start or _utc(prev["orders"]["t"])
    end = end or _utc(cur["orders"]["t"])
    fingerprint = fingerprint_from_stats(prev, cur, start, end)
    if "orders_v2" in prev and "orders_v2" in cur:
        v2 = fingerprint_from_stats(
            {**prev, "orders": prev["orders_v2"]}, {**cur, "orders": cur["orders_v2"]}, start, end
        )
        fingerprint = fingerprint.model_copy(
            update={"services": {**fingerprint.services, "orders_v2": v2.services["orders"]}}
        )
    return fingerprint


def _get_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, method="GET")
    # A container being (re)created binds its port before it listens and resets the
    # connection (RemoteDisconnected, not URLError); every transport failure is "unavailable".
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        raise TelemetryUnavailable(f"telemetry unavailable at {url}") from exc


class LiveTelemetrySource:
    def __init__(
        self,
        orders_url: str = "http://localhost:8101",
        payments_url: str = "http://localhost:8102",
        loadgen_url: str = "http://localhost:8103",
        orders_v2_url: str | None = None,
        timeout_s: float = 3.0,
        retain_s: float = 1800,
        http: Callable[[str, float], dict] = _get_json,
        writer: FingerprintWriter | None = None,
        incident_id: str | None = None,
        clone_id: str | None = None,
    ):
        self._required_urls = {
            "orders": orders_url.rstrip("/") + "/stats",
            "payments": payments_url.rstrip("/") + "/stats",
            "loadgen": loadgen_url.rstrip("/") + "/stats",
        }
        self._optional_urls = (
            {"orders_v2": orders_v2_url.rstrip("/") + "/stats"}
            if orders_v2_url is not None
            else {}
        )
        self._timeout_s = timeout_s
        self._retain_s = retain_s
        self._http = http
        self._writer = writer
        self._incident_id = incident_id
        self._clone_id = clone_id
        self._persisted_windows: set[tuple[datetime, datetime]] = set()
        self._snapshots: deque[tuple[datetime, Snapshot]] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def snapshot(self) -> Snapshot:
        snapshot: Snapshot = {}
        try:
            for service, url in self._required_urls.items():
                snapshot[service] = self._http(url, self._timeout_s)
        except urllib.error.URLError as exc:
            raise TelemetryUnavailable(f"telemetry unavailable at {url}") from exc
        for service, url in self._optional_urls.items():
            try:
                snapshot[service] = self._http(url, self._timeout_s)
            except (TelemetryUnavailable, urllib.error.URLError):
                pass
        timestamp = _utc(snapshot["orders"]["t"])
        with self._lock:
            self._snapshots.append((timestamp, snapshot))
            cutoff = timestamp - timedelta(seconds=self._retain_s)
            while self._snapshots and self._snapshots[0][0] < cutoff:
                self._snapshots.popleft()
        return snapshot

    def start(self, period_s: float = WINDOW_S) -> None:
        if period_s <= 0:
            raise ValueError("period_s must be positive")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(period_s,),
                daemon=True,
                name="faultline-live-telemetry",
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop.set()
        thread.join()
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def window(self, start: datetime, end: datetime) -> Fingerprint:
        with self._lock:
            entries = list(self._snapshots)
        if len(entries) < 2:
            raise ValueError(f"no telemetry covering [{start}, {end})")
        previous_index = next(
            (index for index in range(len(entries) - 1, -1, -1) if entries[index][0] <= start),
            0,
        )
        current_index = next(
            (index for index in range(len(entries) - 1, -1, -1) if entries[index][0] <= end),
            None,
        )
        if current_index is None or previous_index == current_index:
            raise ValueError(f"no telemetry covering [{start}, {end})")
        fingerprint = fingerprint_from_snapshots(
            entries[previous_index][1], entries[current_index][1], start, end
        )
        self._persist(fingerprint)
        return fingerprint

    def series(
        self,
        start: datetime,
        end: datetime,
        step_s: int = WINDOW_S,
    ) -> list[Fingerprint]:
        if step_s <= 0:
            raise ValueError("step_s must be positive")
        step = timedelta(seconds=step_s)
        fingerprints = []
        current = start
        while current + step <= end + timedelta(seconds=0.5):
            try:
                fingerprints.append(self.window(current, current + step))
            except ValueError:
                pass
            current += step
        return fingerprints

    def latest(self) -> Fingerprint | None:
        with self._lock:
            if len(self._snapshots) < 2:
                return None
            previous, current = list(self._snapshots)[-2:]
        fingerprint = fingerprint_from_snapshots(previous[1], current[1])
        self._persist(fingerprint)
        return fingerprint

    def wait_for_breach(
        self, timeout_s: float, poll_s: float = 1.0, sustain_s: float = 0
    ) -> Fingerprint:
        """Return the latest fingerprint once the SLO has been breached continuously for `sustain_s`.

        A single breached window is a transient; acting on it means experimenting while the
        trigger is still active, which confounds the judge (a retry cap during a 20 s DB hiccup
        looks like a degraded DB). The PRD detector is "p99 above threshold for 60 s".
        """
        if timeout_s <= 0:
            raise TimeoutError(f"no SLO breach observed within {timeout_s}s")
        deadline = time.monotonic() + timeout_s
        breached_since: float | None = None
        while True:
            fingerprint = self.latest()
            now = time.monotonic()
            if fingerprint is not None and any(slo.breached for slo in fingerprint.slos):
                breached_since = breached_since if breached_since is not None else now
                if now - breached_since >= sustain_s:
                    return fingerprint
            else:
                breached_since = None
            remaining = deadline - now
            if remaining <= 0:
                raise TimeoutError(f"no SLO breach observed within {timeout_s}s")
            self._stop.wait(min(max(poll_s, 0.01), remaining))

    def healthz(self) -> bool:
        try:
            for url in self._required_urls.values():
                self._fetch(url)
        except TelemetryUnavailable:
            return False
        return True

    def _fetch(self, url: str) -> dict:
        try:
            return self._http(url, self._timeout_s)
        except urllib.error.URLError as exc:
            raise TelemetryUnavailable(f"telemetry unavailable at {url}") from exc

    def _persist(self, fingerprint: Fingerprint) -> None:
        """Index each live C1 window once, regardless of how often consumers read it."""
        if self._writer is None:
            return
        identity = (fingerprint.window_start, fingerprint.window_end)
        with self._lock:
            if identity in self._persisted_windows:
                return
            self._persisted_windows.add(identity)
        try:
            self._writer.write(
                fingerprint, incident_id=self._incident_id, clone_id=self._clone_id
            )
        except Exception:
            with self._lock:
                self._persisted_windows.discard(identity)
            raise

    def _run(self, period_s: float) -> None:
        while not self._stop.is_set():
            try:
                self.snapshot()
            except TelemetryUnavailable:
                pass
            except Exception:  # noqa: BLE001 - the poller must outlive any single bad poll
                log.exception("live telemetry poll failed")
            self._stop.wait(period_s)
