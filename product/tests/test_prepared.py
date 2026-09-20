import importlib.util
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from faultline_contracts.clone import CloneEndpoints, CloneInfo, CloneSpec, CloneStatus

from faultline_product.adapters.prepared import (
    ConcurrentPreparedVerifier,
    PreparedCanaryDeployer,
    PreparedPatchAdapter,
    patch_digest,
)
from faultline_product.cli import main
from faultline_product.fixtures import load_fixture
from faultline_product.paths import REPOSITORY_ROOT
from faultline_product.ports import (
    CanaryPreparationError,
    PatchProposal,
    PatchVerification,
    VerificationStatus,
)

T0 = datetime(2026, 9, 19, 20, 0, tzinfo=timezone.utc)


def _generator():
    path = REPOSITORY_ROOT / "sandbox" / "scripts" / "prepare_demo_patch.py"
    spec = importlib.util.spec_from_file_location("prepare_demo_patch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_source(root: Path) -> Path:
    source = root / "src-tree"
    orders = REPOSITORY_ROOT / "sandbox" / "services" / "orders" / "app.py"
    (source / "sandbox" / "services" / "orders").mkdir(parents=True)
    (source / "sandbox" / "services" / "orders" / "app.py").write_bytes(orders.read_bytes())
    (source / "sandbox" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (source / "sandbox" / "requirements.txt").write_text("fastapi\n")
    (source / "contracts" / "src" / "faultline_contracts").mkdir(parents=True)
    (source / "contracts" / "pyproject.toml").write_text("[project]\nname = \"faultline-contracts\"\n")
    (source / "contracts" / "README.md").write_text("contracts\n")
    (source / "contracts" / "src" / "faultline_contracts" / "__init__.py").write_text("")
    return source


@pytest.fixture
def snapshot(tmp_path):
    module = _generator()
    destination = tmp_path / "snapshot"
    manifest = module.prepare(_make_source(tmp_path), destination)
    return destination, manifest


def _target(patch_ref, status=CloneStatus.ready, name="demo-fast-1"):
    return CloneInfo(
        clone_id=f"{name}-1",
        status=status,
        spec=CloneSpec(name=name, patch_ref=str(patch_ref) if patch_ref else None),
        created_at=T0,
        endpoints=CloneEndpoints(
            gateway_url="http://clone:9080",
            control_url="http://clone:10901",
            stats_urls={
                "orders": "http://clone:9101",
                "payments": "http://clone:9102",
                "loadgen": "http://clone:9103",
                "orders-v2": "http://clone:9104",
            },
        ) if status == CloneStatus.ready else None,
    )


def test_digest_matches_snapshot_generator(snapshot):
    context, manifest = snapshot
    assert manifest["content_sha256"] == patch_digest(context)
    manifest["content_sha256"] = "0" * 64
    (context / "prepared-patch.json").write_text(json.dumps(manifest))
    assert patch_digest(context) != manifest["content_sha256"]


def test_adapter_validates_and_proposes_only_confirmed_h_meta(snapshot):
    context, _ = snapshot
    adapter = PreparedPatchAdapter(context)
    adapter.validate()
    verdict = load_fixture("storm").verdict
    proposal = adapter.propose("inc-1", verdict, None)
    assert proposal.provider == "prepared"
    assert proposal.reference == f"prepared:sha256:{patch_digest(context)}"
    assert adapter.proposal.reference == proposal.reference
    with pytest.raises(ValueError):
        adapter.propose("inc-1", verdict.model_copy(update={"diagnosis": "H_db"}), None)
    with pytest.raises(ValueError):
        adapter.propose("inc-1", verdict.model_copy(update={"confirmed": False}), None)
    assert adapter.revise("inc-1", proposal, "evidence") is None


def test_adapter_rejects_tampered_snapshot(snapshot):
    context, _ = snapshot
    adapter = PreparedPatchAdapter(context)
    target = context / "sandbox" / "services" / "orders" / "app.py"
    target.write_text(target.read_text() + "\n")
    with pytest.raises(ValueError):
        adapter.validate()


def test_adapter_rejects_missing_or_wrong_manifest(snapshot, tmp_path):
    context, _ = snapshot
    manifest = json.loads((context / "prepared-patch.json").read_text())
    manifest["provider"] = "devin"
    (context / "prepared-patch.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        PreparedPatchAdapter(context).validate()
    (context / "prepared-patch.json").write_text(json.dumps(["not", "a", "dict"]))
    with pytest.raises(ValueError):
        PreparedPatchAdapter(context).validate()
    with pytest.raises(ValueError):
        PreparedPatchAdapter(tmp_path / "missing").validate()


def _regenerate_manifest(context: Path) -> None:
    manifest = json.loads((context / "prepared-patch.json").read_text())
    manifest["content_sha256"] = patch_digest(context)
    (context / "prepared-patch.json").write_text(json.dumps(manifest))


def test_adapter_refuses_regenerated_snapshot_and_manifest(snapshot):
    context, _ = snapshot
    adapter = PreparedPatchAdapter(context)
    adapter.validate()
    target = context / "sandbox" / "services" / "orders" / "app.py"
    target.write_text(target.read_text() + "\n")
    _regenerate_manifest(context)
    with pytest.raises(ValueError):
        adapter.validate()


def test_deployer_binds_prepared_snapshot_to_target_v2(snapshot):
    context, _ = snapshot
    patches = PreparedPatchAdapter(context)
    digest = patch_digest(context)
    deployer = PreparedCanaryDeployer(
        patches, _target(context),
        http=lambda url, timeout: {"ok": True, "version": "v2", "prepared_sha256": digest},
    )
    target = deployer.prepare(patches.proposal)
    assert target.version == "v2"
    assert target.service_name == "orders_v2"
    assert target.source_revision == digest
    assert target.patch_reference == patches.proposal.reference


def test_deployer_refuses_mismatched_reference_context_and_health(snapshot, tmp_path):
    context, _ = snapshot
    patches = PreparedPatchAdapter(context)
    healthy = lambda url, timeout: {
        "ok": True, "version": "v2", "prepared_sha256": patch_digest(context)
    }
    deployer = PreparedCanaryDeployer(patches, _target(context), http=healthy)
    with pytest.raises(CanaryPreparationError):
        deployer.prepare(PatchProposal("prepared", "prepared:sha256:other", "x"))
    other = PreparedCanaryDeployer(patches, _target(tmp_path / "elsewhere"), http=healthy)
    with pytest.raises(CanaryPreparationError):
        other.prepare(patches.proposal)
    unready = PreparedCanaryDeployer(patches, _target(context, status=CloneStatus.creating), http=healthy)
    with pytest.raises(CanaryPreparationError):
        unready.prepare(patches.proposal)
    sick = PreparedCanaryDeployer(patches, _target(context), http=lambda u, t: {"ok": True, "version": "v1"})
    with pytest.raises(CanaryPreparationError):
        sick.prepare(patches.proposal)
    missing_v2 = _target(context)
    missing_v2.endpoints.stats_urls.pop("orders-v2")
    with pytest.raises(CanaryPreparationError):
        PreparedCanaryDeployer(patches, missing_v2, http=healthy).prepare(patches.proposal)
    wrong_digest = lambda url, timeout: {"ok": True, "version": "v2", "prepared_sha256": "0" * 64}
    with pytest.raises(CanaryPreparationError):
        PreparedCanaryDeployer(patches, _target(context), http=wrong_digest).prepare(patches.proposal)
    no_digest = lambda url, timeout: {"ok": True, "version": "v2"}
    with pytest.raises(CanaryPreparationError):
        PreparedCanaryDeployer(patches, _target(context), http=no_digest).prepare(patches.proposal)


def test_deployer_refetches_target_from_lab(snapshot):
    context, _ = snapshot
    patches = PreparedPatchAdapter(context)
    calls = []

    class Lab:
        def get(self, clone_id):
            calls.append(clone_id)
            return _target(context)

    deployer = PreparedCanaryDeployer(
        patches, _target(context, status=CloneStatus.creating),
        http=lambda url, timeout: {
            "ok": True, "version": "v2", "prepared_sha256": patch_digest(context)
        },
        lab=Lab(),
    )
    target = deployer.prepare(patches.proposal)
    assert calls == ["demo-fast-1-1"]
    assert target.service_name == "orders_v2"


class _BlockingVerifier:
    def __init__(self, result=None, error=None):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self._result = result or PatchVerification(VerificationStatus.passed, "recovered")
        self._error = error

    def verify(self, incident_id, patch, diagnosis, context=None):
        self.calls.append((incident_id, patch.reference, diagnosis, context))
        self.started.set()
        self.release.wait(10)
        if self._error is not None:
            raise self._error
        return self._result


def test_concurrent_verifier_starts_nonblocking_and_verify_waits(snapshot):
    context, _ = snapshot
    patches = PreparedPatchAdapter(context)
    inner = _BlockingVerifier()
    verifier = ConcurrentPreparedVerifier(inner, patches)
    verifier.start("inc-1")
    assert inner.started.wait(2)
    assert inner.calls == [("inc-1", patches.proposal.reference, "H_meta", patches.context)]
    outcome = {}

    def call():
        outcome["result"] = verifier.verify("inc-1", patches.proposal, "H_meta", context)

    thread = threading.Thread(target=call)
    thread.start()
    thread.join(0.5)
    assert thread.is_alive()
    inner.release.set()
    thread.join(10)
    assert outcome["result"].status == VerificationStatus.passed
    verifier.close()


def test_concurrent_verifier_refuses_missing_or_mismatched_calls(snapshot):
    context, _ = snapshot
    patches = PreparedPatchAdapter(context)
    verifier = ConcurrentPreparedVerifier(_BlockingVerifier(), patches)
    assert verifier.verify("inc-1", patches.proposal, "H_meta").status == VerificationStatus.failed
    verifier.start("inc-1")
    wrong = verifier.verify("inc-2", patches.proposal, "H_meta")
    assert wrong.status == VerificationStatus.failed
    wrong_diag = verifier.verify("inc-1", patches.proposal, "H_db")
    assert wrong_diag.status == VerificationStatus.failed
    wrong_ref = verifier.verify(
        "inc-1", PatchProposal("prepared", "prepared:sha256:other", "x"), "H_meta"
    )
    assert wrong_ref.status == VerificationStatus.failed
    verifier._verifier.release.set()
    verifier.close()


def test_concurrent_verifier_maps_skipped_and_raised_to_failed(snapshot):
    context, _ = snapshot
    patches = PreparedPatchAdapter(context)
    skipped = ConcurrentPreparedVerifier(
        _BlockingVerifier(result=PatchVerification(VerificationStatus.skipped, "no lab")), patches)
    skipped.start("inc-1")
    skipped._verifier.release.set()
    result = skipped.verify("inc-1", patches.proposal, "H_meta")
    assert result.status == VerificationStatus.failed and "did not pass" in result.detail
    skipped.close()
    raised = ConcurrentPreparedVerifier(_BlockingVerifier(error=RuntimeError("boom")), patches)
    raised.start("inc-1")
    raised._verifier.release.set()
    result = raised.verify("inc-1", patches.proposal, "H_meta")
    assert result.status == VerificationStatus.failed and "RuntimeError" in result.detail
    raised.close()


def test_concurrent_verifier_refuses_mutated_context(snapshot):
    context, _ = snapshot
    patches = PreparedPatchAdapter(context)
    inner = _BlockingVerifier()
    verifier = ConcurrentPreparedVerifier(inner, patches)
    proposal = patches.proposal
    verifier.start("inc-1")
    target = context / "sandbox" / "services" / "orders" / "app.py"
    target.write_text(target.read_text() + "\n")
    inner.release.set()
    assert verifier.verify("inc-1", proposal, "H_meta").status == VerificationStatus.failed
    verifier.close()


def test_concurrent_verifier_refuses_snapshot_changed_while_replaying(snapshot):
    context, _ = snapshot
    patches = PreparedPatchAdapter(context)
    inner = _BlockingVerifier()
    verifier = ConcurrentPreparedVerifier(inner, patches)
    proposal = patches.proposal
    verifier.start("inc-1")
    target = context / "sandbox" / "services" / "orders" / "app.py"
    target.write_text(target.read_text() + "\n")
    _regenerate_manifest(context)
    inner.release.set()
    result = verifier.verify("inc-1", proposal, "H_meta")
    assert result.status == VerificationStatus.failed
    verifier.close()


def test_failed_prepared_result_keeps_measured_evidence(snapshot):
    context, _ = snapshot
    patches = PreparedPatchAdapter(context)
    inner = _BlockingVerifier(result=PatchVerification(
        VerificationStatus.skipped, "clone lab unavailable",
        clone_id="verify-inc-1", recipe={"action": "db_latency"}, evidence={"w": 1}))
    verifier = ConcurrentPreparedVerifier(inner, patches)
    verifier.start("inc-1")
    inner.release.set()
    result = verifier.verify("inc-1", patches.proposal, "H_meta")
    assert result.status == VerificationStatus.failed
    assert result.clone_id == "verify-inc-1"
    assert result.recipe == {"action": "db_latency"}
    assert result.evidence == {"w": 1}
    verifier.close()


def test_write_ready_is_exclusive_and_atomic(tmp_path):
    from faultline_product.prepared import _write_ready

    path = tmp_path / "ready.json"
    _write_ready(path, {"a": 1})
    with pytest.raises(FileExistsError):
        _write_ready(path, {"a": 2})
    path2 = tmp_path / "ready2.json"
    path2.write_text("{}")
    with pytest.raises(FileExistsError):
        _write_ready(path2, {"a": 1})


def test_prepared_watch_preflight_never_pagers_or_network(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("FAULTLINE_PAGER_WEBHOOK", "http://pager.invalid/hook")
    audit = tmp_path / "audit.jsonl"
    args = [
        "--audit-log", str(audit), "prepared-watch",
        "--incident", "fast-x", "--lab-url", "http://127.0.0.1:9",
        "--target-clone-id", "demo-x-1", "--patch-context", str(tmp_path / "snap"),
        "--ready-file", str(tmp_path / "ready.json"),
    ]
    assert main(args) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().out
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert main(args) == 2
    bad_ready = tmp_path / "ready.json"
    bad_ready.write_text("{}")
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "prepared-patch.json").write_text("{}")
    assert main(args) == 2


def test_prepared_watch_parser_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    audit = tmp_path / "audit.jsonl"
    base = [
        "--audit-log", str(audit), "prepared-watch",
        "--incident", "fast-x", "--lab-url", "http://127.0.0.1:9",
        "--target-clone-id", "demo-x-1", "--patch-context", str(tmp_path),
        "--ready-file", str(tmp_path / "ready.json"),
    ]
    assert main([*base, "--baseline-s", "60"]) == 2
    assert main([*base, "--baseline-s", "nan"]) == 2
    assert main([*base, "--baseline-s", "inf"]) == 2
    assert main([*base, "--detect-timeout", "0"]) == 2
    assert main([*base, "--detect-timeout", "nan"]) == 2
    with pytest.raises(SystemExit):
        main([*base, "--detect-timeout", "-inf"])
    with pytest.raises(SystemExit):
        main([*base, "--baseline-s", "abc"])
