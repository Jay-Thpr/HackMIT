"""Fairness boundary for Product runtime code."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT / "src"


def _imports_fault(tree: ast.AST) -> list[str]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name.startswith("faultline_contracts.fault")]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("faultline_contracts.fault") or (
                mod == "faultline_contracts" and any(a.name == "fault" for a in node.names)
            ):
                hits.append(mod)
            if node.level > 0 and (
                mod == "fault"
                or mod.startswith("fault.")
                or (mod == "" and any(a.name == "fault" for a in node.names))
            ):
                hits.append("." * node.level + mod)
    return hits


def _imports_fakes(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name.startswith("faultline_contracts.fakes") for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("faultline_contracts.fakes") or (
                mod == "faultline_contracts" and any(a.name == "fakes" for a in node.names)
            ):
                return True
    return False


def test_product_runtime_does_not_import_hidden_fault_controller_or_fakes():
    bad_fault = {}
    bad_fakes = []
    for py in RUNTIME_DIR.rglob("*.py"):
        src = py.read_text()
        hits = _imports_fault(ast.parse(src))
        if "faultline_contracts.fault" in src.replace("faultline_contracts.fakes", ""):
            hits.append("string reference")
        if hits:
            bad_fault[str(py)] = hits
        if _imports_fakes(ast.parse(src)):
            bad_fakes.append(str(py))
    assert not bad_fault, bad_fault
    assert not bad_fakes, bad_fakes


def test_detector_itself():
    assert _imports_fakes(ast.parse("from faultline_contracts.fakes import FakeWorld"))
    assert _imports_fault(ast.parse("from faultline_contracts.fault import World"))
    assert not _imports_fault(ast.parse("from faultline_contracts import Fingerprint"))
