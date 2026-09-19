from pathlib import Path

import pytest

from faultline_product.adapters.canary import (
    CanaryPreparationError,
    FixtureCanaryDeployer,
    SandboxCanaryDeployer,
)
from faultline_product.ports import PatchProposal


def _patch():
    return PatchProposal("devin", "https://github.test/pull/1", "bounded retries")


def test_fixture_canary_is_tied_to_patch_reference():
    target = FixtureCanaryDeployer().prepare(_patch())
    assert target.patch_reference == _patch().reference
    assert target.source_revision == "fixture"


def test_sandbox_canary_requires_patched_checkout(tmp_path):
    deployer = SandboxCanaryDeployer(compose_dir=tmp_path, context=None)
    with pytest.raises(CanaryPreparationError, match="--canary-context"):
        deployer.prepare(_patch())


def test_sandbox_canary_builds_context_and_records_revision(tmp_path):
    calls = []

    def runner(command, *, cwd: Path, env: dict[str, str]):
        calls.append((command, cwd, env))

    deployer = SandboxCanaryDeployer(
        compose_dir=tmp_path,
        context=tmp_path,
        runner=runner,
        http=lambda url, timeout: {"ok": True, "version": "v2"},
        revision=lambda path: "abc123",
        sleep=lambda seconds: None,
    )

    target = deployer.prepare(_patch())

    assert calls[0][0][-1] == "orders-v2"
    assert calls[0][2]["ORDERS_V2_CONTEXT"] == str(tmp_path.resolve())
    assert target.patch_reference == _patch().reference
    assert target.source_revision == "abc123"
    assert target.service_name == "orders_v2"
