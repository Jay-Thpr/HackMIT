"""Outbound retry policy for the checkout API's downstream calls (inventory, payments, cache).

Every downstream client asks ``RetryPolicy.should_retry`` before re-issuing a request and
``RetryPolicy.delay_s`` for how long to wait. The defaults below are the pre-incident
production values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = int(os.environ.get("API_MAX_RETRIES", "3"))
    attempt_timeout_s: float = float(os.environ.get("API_ATTEMPT_TIMEOUT_MS", "500")) / 1000.0

    def should_retry(self, attempt: int, status: int | None, timed_out: bool) -> bool:
        if attempt > self.max_retries:
            return False
        return timed_out or status is None or status >= 500

    def delay_s(self, attempt: int) -> float:
        del attempt
        return 0.0


DEFAULT_POLICY = RetryPolicy()
