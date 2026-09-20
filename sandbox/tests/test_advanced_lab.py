import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from faultline_contracts.clone import CloneSpec  # noqa: E402

from advanced import lab  # noqa: E402


class LabTest(unittest.TestCase):
    def setUp(self):
        self._saved = {"clones": dict(lab._state["clones"]),
                       "forwards": dict(lab._state["forwards"])}
        lab._state["clones"] = {}
        lab._state["forwards"] = {}
        self.addCleanup(lambda: lab._state.update(
            {"clones": self._saved["clones"], "forwards": self._saved["forwards"]}))

    def test_catalog_five_clone_only_actions(self):
        result = asyncio.run(lab.catalog())
        self.assertEqual({a["id"] for a in result},
                         {"worker_delay", "replica_pause", "workload",
                          "cache_policy", "cpu_limit"})
        self.assertTrue(all(a["max_ttl_s"] == 120 for a in result))

    def test_versions_and_patch_ref_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(lab.create(CloneSpec(name="x", patch_ref="/some/path")))
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(lab.create(CloneSpec(name="x")))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("advanced", ctx.exception.detail)

    def _record(self, status="ready", uid="uid-1", expires=0):
        return {
            "namespace": "faultline-advanced-clone-a1b2c3d4",
            "namespace_uid": uid,
            "spec": CloneSpec(name="x", versions={"advanced": "v1"}).model_dump(mode="json"),
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires,
            "port": None,
            "actions": [],
        }

    def test_capacity_and_destroy_uid_guard(self):
        cid = "adv-0123456789"
        lab._state["clones"][cid] = self._record()
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(lab.create(CloneSpec(name="y", versions={"advanced": "v1"})))
        self.assertEqual(ctx.exception.status_code, 409)

        def fake_kubectl(args, **k):
            if "get" in args and "-o" in args:
                return json.dumps({"metadata": {"uid": "different"}})
            if "get" in args:
                return json.dumps({"metadata": {"uid": "different"}})
            return ""

        with patch.object(lab, "_kubectl", fake_kubectl):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(lab.destroy(cid))
            self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(lab._state["clones"][cid]["status"], "quarantined")

        lab._state["clones"][cid]["status"] = "ready"

        def fake_kubectl_ok(args, **k):
            if "get" in args and "-o" in args:
                return json.dumps({"metadata": {"uid": "uid-1"}})
            if "get" in args:
                return ""
            return ""

        with patch.object(lab, "_kubectl", fake_kubectl_ok), \
                patch.object(lab, "STATE_FILE", Path(tempfile.mkdtemp()) / "s.json"):
            info = asyncio.run(lab.destroy(cid))
        self.assertEqual(info["status"], "destroyed")

    def test_deletion_pending_preserves_capacity(self):
        cid = "adv-0123456789"
        lab._state["clones"][cid] = self._record()

        def fake_kubectl(args, **k):
            if "get" in args:
                return json.dumps({"metadata": {"uid": "uid-1"}})
            return ""

        with patch.object(lab, "_kubectl", fake_kubectl), \
                patch.object(lab, "STATE_FILE", Path(tempfile.mkdtemp()) / "s.json"), \
                patch.object(lab, "DESTROY_POLL_S", 0.05):
            asyncio.run(lab.destroy(cid))
        self.assertEqual(lab._state["clones"][cid]["status"], "cleanup_pending")
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(lab.create(CloneSpec(name="y", versions={"advanced": "v1"})))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_unknown_uid_namespace_never_deleted(self):
        cid = "adv-0123456789"
        record = self._record()
        record["namespace_uid"] = None
        lab._state["clones"][cid] = record
        calls = []

        def fake_kubectl(args, **k):
            calls.append(args)
            if "get" in args:
                return json.dumps({"metadata": {"uid": "someone-else"}})
            return ""

        with patch.object(lab, "_kubectl", fake_kubectl), \
                patch.object(lab, "STATE_FILE", Path(tempfile.mkdtemp()) / "s.json"):
            asyncio.run(lab._destroy(cid))
        self.assertEqual(lab._state["clones"][cid]["status"], "quarantined")
        self.assertFalse(any("delete" in c for c in calls))

    def test_namespace_get_error_retains_capacity(self):
        cid = "adv-0123456789"
        lab._state["clones"][cid] = self._record()

        def fake_kubectl(args, **k):
            raise RuntimeError("api down")

        with patch.object(lab, "_kubectl", fake_kubectl), \
                patch.object(lab, "STATE_FILE", Path(tempfile.mkdtemp()) / "s.json"):
            asyncio.run(lab._destroy(cid))
        self.assertEqual(lab._state["clones"][cid]["status"], "cleanup_pending")
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(lab.create(CloneSpec(name="y", versions={"advanced": "v1"})))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_main_namespace_record_quarantined_no_delete(self):
        cid = "adv-0123456789"
        record = self._record()
        record["namespace"] = "faultline-advanced"
        record["namespace_uid"] = "uid-1"
        lab._state["clones"][cid] = record
        calls = []

        def fake_kubectl(args, **k):
            calls.append(args)
            return json.dumps({"metadata": {"uid": "uid-1"}})

        with patch.object(lab, "_kubectl", fake_kubectl), \
                patch.object(lab, "STATE_FILE", Path(tempfile.mkdtemp()) / "s.json"):
            asyncio.run(lab._destroy(cid))
        self.assertEqual(lab._state["clones"][cid]["status"], "quarantined")
        self.assertFalse(any("delete" in c for c in calls))

    def test_malformed_state_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lab-state.json"
            path.write_text(json.dumps({"not-a-clone-id": {"namespace": "prod"}}))
            with patch.object(lab, "STATE_FILE", path):
                lab._load()
                self.assertTrue(lab._state["load_error"])
        lab._state["load_error"] = False

    def test_workload_body_rejects_zero_rps(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            lab.WorkloadBody(rps=0)
        self.assertEqual(lab.WorkloadBody(rps=10).rps, 10)

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lab-state.json"
            path.write_text("{corrupt")
            with patch.object(lab, "STATE_FILE", path):
                lab._load()
                self.assertTrue(lab._state["load_error"])
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(lab.create(CloneSpec(
                        name="y", versions={"advanced": "v1"})))
                self.assertEqual(ctx.exception.status_code, 503)
        lab._state["load_error"] = False

    def test_creation_failure_retains_record(self):
        def fake_kubectl(args, **k):
            raise RuntimeError("api down")

        with patch.object(lab, "_kubectl", fake_kubectl), \
                patch.object(lab, "STATE_FILE", Path(tempfile.mkdtemp()) / "s.json"), \
                patch.object(lab, "_preflight", lambda: None):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(lab.create(CloneSpec(name="y", versions={"advanced": "v1"})))
            self.assertEqual(ctx.exception.status_code, 503)
        records = list(lab._state["clones"].values())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "failed")

    def test_unknown_clone_404(self):
        for call in (lab.get, lab.destroy, lab.actions):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(call("missing"))
            self.assertEqual(ctx.exception.status_code, 404)

    def test_state_file_atomic_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lab-state.json"
            with patch.object(lab, "STATE_FILE", path):
                lab._state["clones"]["c1"] = {"namespace": "n"}
                lab._save()
            import stat
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text())["c1"]["namespace"], "n")


if __name__ == "__main__":
    unittest.main()
