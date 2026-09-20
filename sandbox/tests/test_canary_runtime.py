import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.control import app as control  # noqa: E402

CANARY_KEY = control.CANARY_KEY
SHED_KEY = control.SHED_KEY


class FakeHttp:
    def __init__(self):
        self.posts = []
        self.deleted = []

    async def post(self, url, params=None, **kw):
        self.posts.append((url, dict(params or {})))
        return SimpleNamespace(raise_for_status=lambda: None)

    async def delete(self, url, **kw):
        self.deleted.append(url)
        return SimpleNamespace(raise_for_status=lambda: None)

    async def get(self, url, **kw):
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"override": None})


def _canary_values(http):
    return [p[CANARY_KEY] for url, p in http.posts if CANARY_KEY in p]


class CanaryRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        self._http = control.http
        control.http = self.http
        self.addCleanup(lambda: setattr(control, "http", self._http))
        for lv in control.levers.values():
            lv.params, lv.active, lv.expires_at = {}, False, None

    def test_runtime_value_fraction_json(self):
        cases = [
            (0.05, 500), (0.0, 0), (1.0, 10000), (0.0001, 1), (0.375, 3750),
        ]
        for weight, numerator in cases:
            raw = control.canary_runtime_value(weight)
            parsed = json.loads(raw)
            self.assertEqual(parsed, {"numerator": numerator,
                                      "denominator": "TEN_THOUSAND"}, weight)
            self.assertIsInstance(parsed["numerator"], int)

    def test_push_writes_fraction_not_percent(self):
        asyncio.run(control.push("canary_weight", {"v2_weight": 0.05}, 60))
        values = _canary_values(self.http)
        self.assertEqual(len(values), 1)
        parsed = json.loads(values[0])
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed, {"numerator": 500, "denominator": "TEN_THOUSAND"})

    def test_revert_and_reconcile_write_fraction_json(self):
        asyncio.run(control.revert("canary_weight"))
        parsed = json.loads(_canary_values(self.http)[-1])
        self.assertEqual(parsed, {"numerator": 0, "denominator": "TEN_THOUSAND"})

        class StopLoop(Exception):
            pass

        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)
            raise StopLoop

        shed = control.levers["shed"]
        shed.params, shed.active = {"fraction": 0.5}, True
        canary = control.levers["canary_weight"]
        canary.params, canary.active = {"v2_weight": 0.05}, True
        with patch("asyncio.sleep", fake_sleep):
            with self.assertRaises(StopLoop):
                asyncio.run(control._reconcile_loop())
        flat = {k: v for _, p in self.http.posts for k, v in p.items()}
        self.assertEqual(flat[SHED_KEY], "50")
        self.assertEqual(json.loads(flat[CANARY_KEY]),
                         {"numerator": 500, "denominator": "TEN_THOUSAND"})

        self.http.posts.clear()
        canary.active = False
        slept.clear()
        with patch("asyncio.sleep", fake_sleep):
            with self.assertRaises(StopLoop):
                asyncio.run(control._reconcile_loop())
        flat = {k: v for _, p in self.http.posts for k, v in p.items()}
        self.assertEqual(json.loads(flat[CANARY_KEY]),
                         {"numerator": 0, "denominator": "TEN_THOUSAND"})


if __name__ == "__main__":
    unittest.main()
