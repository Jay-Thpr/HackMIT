import asyncio
import importlib
import os
import unittest
from unittest.mock import patch

from faultline_contracts.clone import CloneSpec


def _load_lab(**env):
    with patch.dict(os.environ, env, clear=False):
        import services.lab.app as lab
        return importlib.reload(lab)


class LabIsolationTest(unittest.TestCase):
    def tearDown(self):
        _load_lab()

    def test_defaults_keep_existing_behavior(self):
        lab = _load_lab()
        self.assertEqual(lab.CLONE_PROJECT_PREFIX, "faultline-clone-")
        self.assertEqual(lab.PORT_OFFSET, 0)
        clone = lab.Clone("c1", 1, CloneSpec(name="demo-x"))
        self.assertEqual(clone.project, "faultline-clone-1")
        self.assertEqual(clone.ports["gateway"], 9080)
        self.assertEqual(clone.ports["control"], 10901)
        self.assertEqual(clone.env()["ORDERS_V2_IMAGE"], "faultline-clone-1-orders-v2:prepared")

    def test_demo_prefix_and_port_offset_isolate_projects(self):
        lab = _load_lab(LAB_PROJECT_PREFIX="faultline-demo-", LAB_PORT_OFFSET="20000")
        clone = lab.Clone("c1", 2, CloneSpec(name="demo-y", patch_ref="/tmp/snap"))
        self.assertEqual(clone.project, "faultline-demo-2")
        self.assertEqual(clone.ports["gateway"], 8080 + 20000 + 2000)
        self.assertEqual(clone.ports["control"], 9901 + 20000 + 2000)
        self.assertEqual(clone.env()["ORDERS_V2_IMAGE"], "faultline-demo-2-orders-v2:prepared")

    def test_invalid_prefix_rejected(self):
        for bad in ("evil-", "faultline-prod-", "faultline-clone", ""):
            with self.assertRaises(ValueError, msg=bad):
                _load_lab(LAB_PROJECT_PREFIX=bad)

    def test_valid_prefixes_accepted(self):
        _load_lab(LAB_PROJECT_PREFIX="faultline-clone-")
        _load_lab(LAB_PROJECT_PREFIX="faultline-demo-")
        _load_lab(LAB_PROJECT_PREFIX="faultline-demo-team1-")

    def test_invalid_port_offset_rejected(self):
        for bad in ("-1", "60000", "56000"):
            with self.assertRaises(ValueError, msg=bad):
                _load_lab(LAB_PORT_OFFSET=bad)

    def test_healthz_reports_isolation_metadata(self):
        lab = _load_lab(LAB_PROJECT_PREFIX="faultline-demo-", LAB_PORT_OFFSET="20000")
        body = asyncio.run(lab.healthz())
        self.assertEqual(body["project_prefix"], "faultline-demo-")
        self.assertEqual(body["port_offset"], 20000)
        self.assertTrue(body["ok"])
        self.assertIn("clones_alive", body)
        self.assertIn("max_clones", body)


if __name__ == "__main__":
    unittest.main()
