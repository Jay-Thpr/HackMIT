"""Undoing the retry_cap lever (DELETE :9901 or a C5 reset) must really restore Orders'
default retry policy — control may not report the lever inactive while Orders is still
capped. Skipped unless the sandbox is reachable.

  cd integration && uv run pytest -q tests/test_retry_cap_reset.py
"""

import os
import sys
import time
from pathlib import Path

import httpx
import pytest
from faultline_contracts.fault import HttpFaultController

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import smoke_sandbox as sm  # noqa: E402

DEFAULT = int(os.environ.get("ORDERS_MAX_RETRIES", "3"))


def _up() -> bool:
    try:
        return (httpx.get(f"{sm.CONTROL_URL}/healthz", timeout=2).status_code == 200
                and httpx.get(f"{sm.FAULT_URL}/fault/state", timeout=2).status_code == 200)
    except httpx.HTTPError:
        return False


def orders_max_retries() -> int | None:
    return httpx.get(f"{sm.STATS_URLS['orders']}/stats", timeout=5).json().get("gauges", {}).get("max_retries")


def levers() -> dict:
    r = httpx.get(f"{sm.CONTROL_URL}/admin/levers", timeout=5)
    r.raise_for_status()
    return r.json()


def wait_for(pred, timeout_s: float, step: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


@pytest.fixture(scope="module")
def clean():
    if _up():
        HttpFaultController(sm.FAULT_URL, timeout_s=150).reset()  # clear any stale storm first


@pytest.mark.skipif(not _up(), reason="sandbox not running on :9900/:9901")
@pytest.mark.parametrize("undo", ["delete", "c5_reset"])
def test_retry_cap_undo_restores_orders_policy(undo, clean):
    fc = HttpFaultController(sm.FAULT_URL, timeout_s=150)
    sampler = sm.Sampler(verbose=False)

    assert orders_max_retries() == DEFAULT

    r = httpx.post(f"{sm.CONTROL_URL}/admin/retry_override", json={"max_retries": 0, "ttl_s": 60}, timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True

    assert wait_for(lambda: orders_max_retries() == 0, 3), f"orders max_retries={orders_max_retries()}"

    if undo == "delete":
        r = httpx.delete(f"{sm.CONTROL_URL}/admin/retry_override", timeout=10)
        assert r.status_code == 200, r.text
    else:
        fc.reset()

    assert levers()["retry_cap"]["active"] is False
    assert wait_for(lambda: orders_max_retries() == DEFAULT, 3), \
        f"orders max_retries={orders_max_retries()} still not back to {DEFAULT}"

    # idempotent undo: deleting an inactive lever is a no-op, not an error
    r = httpx.delete(f"{sm.CONTROL_URL}/admin/retry_override", timeout=10)
    assert r.status_code == 200, r.text
    assert levers()["retry_cap"]["active"] is False
    assert orders_max_retries() == DEFAULT

    time.sleep(2)  # settle
    span = sampler.run_for(5, undo)
    m = sampler.tail(span, 5)
    assert sm.is_healthy(m), sm.fmt(m)
    assert m["orders.max_retries"] == DEFAULT


@pytest.mark.skipif(not _up(), reason="sandbox not running on :9900/:9901")
def test_retry_cap_ttl_expiry_restores_orders_policy(clean):
    r = httpx.post(f"{sm.CONTROL_URL}/admin/retry_override", json={"max_retries": 0, "ttl_s": 2}, timeout=10)
    assert r.status_code == 200, r.text

    assert wait_for(lambda: orders_max_retries() == 0, 3), f"orders max_retries={orders_max_retries()}"
    assert wait_for(lambda: levers()["retry_cap"]["active"] is False, 6)
    assert wait_for(lambda: orders_max_retries() == DEFAULT, 3), \
        f"orders max_retries={orders_max_retries()} still not back to {DEFAULT}"

    r = httpx.delete(f"{sm.CONTROL_URL}/admin/retry_override", timeout=10)
    assert r.status_code == 200, r.text
    assert orders_max_retries() == DEFAULT
