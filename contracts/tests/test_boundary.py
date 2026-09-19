"""Fairness boundary: Faultline must never see the fault controller (C5) or world labels."""

import ast
import json
from pathlib import Path

import pytest

import faultline_contracts

ROOT = Path(__file__).resolve().parent.parent
FAULTLINE_DIR = ROOT.parent / "faultline"
FORBIDDEN_WORDS = ("world", "storm", "degraded", "fault", "cpu_starve")


def _imports_fault(tree: ast.AST) -> list[str]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits += [a.name for a in node.names if a.name.startswith("faultline_contracts.fault")]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("faultline_contracts.fault") or (
                mod.endswith("faultline_contracts") and any(a.name == "fault" for a in node.names)
            ):
                hits.append(mod)
            if node.level > 0 and (mod == "fault" or mod.startswith("fault.") or
                                   (mod == "" and any(a.name == "fault" for a in node.names))):
                hits.append("." * node.level + mod)
    return hits


def test_faultline_does_not_import_fault_controller():
    if not FAULTLINE_DIR.is_dir():
        pytest.skip("faultline/ not created yet")
    bad = {}
    for py in FAULTLINE_DIR.rglob("*.py"):
        if ".venv" in py.parts:
            continue
        src = py.read_text()
        hits = _imports_fault(ast.parse(src))
        if "faultline_contracts.fault" in src.replace("faultline_contracts.fakes", ""):
            hits.append("string reference")
        if hits:
            bad[str(py)] = hits
    assert not bad, f"Faultline imports the hidden fault controller: {bad}"


def _imports_fakes(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.startswith("faultline_contracts.fakes") for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("faultline_contracts.fakes") or (
                mod == "faultline_contracts" and any(a.name == "fakes" for a in node.names)
            ):
                return True
    return False


def test_faultline_runtime_does_not_import_fakes():
    """fakes/ wraps the fault controller, so only Faultline's tests may use it."""
    if not FAULTLINE_DIR.is_dir():
        pytest.skip("faultline/ not created yet")
    bad = [
        str(py)
        for py in FAULTLINE_DIR.rglob("*.py")
        if ".venv" not in py.parts
        and "tests" not in py.parts
        and not py.name.startswith("test_")
        and py.name != "conftest.py"
        and _imports_fakes(ast.parse(py.read_text()))
    ]
    assert not bad, f"Faultline runtime code imports fakes (which wrap the fault controller): {bad}"


def test_detector_itself():
    assert _imports_fakes(ast.parse("from faultline_contracts.fakes import FakeWorld"))
    assert _imports_fakes(ast.parse("from faultline_contracts import fakes"))
    assert not _imports_fakes(ast.parse("from faultline_contracts import Fingerprint"))
    assert _imports_fault(ast.parse("from faultline_contracts.fault import World"))
    assert _imports_fault(ast.parse("from faultline_contracts import fault"))
    assert _imports_fault(ast.parse("import faultline_contracts.fault as f"))
    assert _imports_fault(ast.parse("from .fault import World"))
    assert not _imports_fault(ast.parse("from faultline_contracts import Fingerprint"))


def test_package_does_not_reexport_fault():
    import faultline_contracts.fault as fault

    fault_names = {n for n in vars(fault) if not n.startswith("_")} - set(dir(__import__("typing"))) - {
        "datetime", "Enum", "Any", "Protocol", "runtime_checkable", "httpx", "Field", "Model"}
    exported = set(dir(faultline_contracts))
    assert not (fault_names & exported), fault_names & exported
    init_src = (ROOT / "src" / "faultline_contracts" / "__init__.py").read_text()
    assert not _imports_fault(ast.parse(init_src))
    assert "from .fault" not in init_src and "import fault" not in init_src


def _strings(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


@pytest.mark.parametrize("name", [
    "fingerprint_healthy.json", "fingerprint_storm.json", "fingerprint_degraded_db.json",
    "series_storm_experiment.json", "series_degraded_db_experiment.json",
])
def test_fingerprint_fixtures_leak_no_world_labels(name):
    data = json.loads((ROOT / "fixtures" / name).read_text())
    leaks = {s for s in _strings(data) for w in FORBIDDEN_WORDS if w in s.lower()}
    assert not leaks, f"{name} leaks hidden labels: {leaks}"
