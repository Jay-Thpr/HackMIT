import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT / "scripts"))

import prepare_demo_patch as pdp  # noqa: E402

REAL_ORDERS = ROOT / "services" / "orders" / "app.py"


def make_source(root: Path) -> Path:
    source = root / "src-tree"
    (source / "sandbox" / "services" / "orders").mkdir(parents=True)
    (source / "sandbox" / "services" / "orders" / "app.py").write_bytes(REAL_ORDERS.read_bytes())
    (source / "sandbox" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (source / "sandbox" / "requirements.txt").write_text("fastapi\n")
    (source / "sandbox" / "services" / ".env").write_text("SECRET=1\n")
    (source / "sandbox" / "services" / ".env.local").write_text("SECRET=2\n")
    (source / "sandbox" / "services" / "__pycache__").mkdir()
    (source / "sandbox" / "services" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"x")
    (source / "contracts" / "src" / "faultline_contracts").mkdir(parents=True)
    (source / "contracts" / "pyproject.toml").write_text("[project]\nname = \"faultline-contracts\"\n")
    (source / "contracts" / "README.md").write_text("contracts\n")
    (source / "contracts" / "src" / "faultline_contracts" / "__init__.py").write_text("")
    return source


class PrepareDemoPatchTest(unittest.TestCase):
    def test_prepare_writes_manifest_and_patched_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source(Path(tmp))
            destination = Path(tmp) / "snapshot"
            manifest = pdp.prepare(source, destination)
            self.assertEqual(manifest["schema_version"], "faultline-prepared-patch/1")
            self.assertEqual(manifest["provider"], "prepared")
            self.assertEqual(manifest["replay_profile"], "retry-storm-v1")
            self.assertEqual(manifest["content_sha256"], pdp.patch_digest(destination))
            written = json.loads((destination / "prepared-patch.json").read_text())
            self.assertEqual(written["content_sha256"], manifest["content_sha256"])
            patched = (destination / "sandbox" / "services" / "orders" / "app.py").read_text()
            compile(patched, "app.py", "exec")
            self.assertIn("import random", patched)
            self.assertIn("import json", patched)
            self.assertIn("_retry_tokens = 10.0", patched)
            self.assertIn("retries_budget_denied", patched)
            self.assertIn("prepared_sha256", patched)
            dockerfile = (destination / "sandbox" / "Dockerfile").read_text()
            self.assertIn("COPY prepared-patch.json /app/prepared-patch.json", dockerfile)

    def test_source_orders_app_is_not_modified(self):
        original = REAL_ORDERS.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source(Path(tmp))
            before = (source / "sandbox" / "services" / "orders" / "app.py").read_bytes()
            pdp.prepare(source, Path(tmp) / "snapshot")
            self.assertEqual((source / "sandbox" / "services" / "orders" / "app.py").read_bytes(), before)
        self.assertEqual(REAL_ORDERS.read_bytes(), original)

    def test_existing_destination_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source(Path(tmp))
            destination = Path(tmp) / "snapshot"
            destination.mkdir()
            with self.assertRaises(ValueError):
                pdp.prepare(source, destination)

    def test_digest_changes_when_content_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source(Path(tmp))
            destination = Path(tmp) / "snapshot"
            manifest = pdp.prepare(source, destination)
            target = destination / "sandbox" / "services" / "orders" / "app.py"
            target.write_text(target.read_text() + "\n")
            self.assertNotEqual(pdp.patch_digest(destination), manifest["content_sha256"])

    def test_env_and_cache_files_are_not_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source(Path(tmp))
            destination = Path(tmp) / "snapshot"
            pdp.prepare(source, destination)
            copied = [p.relative_to(destination).as_posix() for p in destination.rglob("*")]
            self.assertFalse(any(".env" in name or "__pycache__" in name for name in copied), copied)

    def test_symlinks_in_source_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source(Path(tmp))
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "secret.py").write_text("x")
            (source / "sandbox" / "services" / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                pdp.prepare(source, Path(tmp) / "snapshot")

    def test_file_symlink_in_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source(Path(tmp))
            (source / "sandbox" / "services" / "linked.py").symlink_to(Path(tmp) / "src-tree" / "sandbox" / "requirements.txt")
            with self.assertRaises(ValueError):
                pdp.prepare(source, Path(tmp) / "snapshot")

    def test_retry_budget_functions_behave(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source(Path(tmp))
            destination = Path(tmp) / "snapshot"
            pdp.prepare(source, destination)
            text = (destination / "sandbox" / "services" / "orders" / "app.py").read_text()
            block = text[text.index("def _budget_credit"):text.index("def _override_live")]
            ns: dict = {"_retry_tokens": 0.0}
            exec(compile(block, "app.py", "exec"), ns)
            ns["_budget_credit"]()
            self.assertAlmostEqual(ns["_retry_tokens"], 0.1)
            self.assertFalse(ns["_budget_take"]())
            ns["_retry_tokens"] = 1.5
            self.assertTrue(ns["_budget_take"]())
            self.assertAlmostEqual(ns["_retry_tokens"], 0.5)
            ns["_retry_tokens"] = 10.0
            ns["_budget_credit"]()
            self.assertEqual(ns["_retry_tokens"], 10.0)


if __name__ == "__main__":
    unittest.main()
