from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from faultline_contracts import ActionHandle, UndoSpec
from faultline_contracts.clone import (
    CloneEndpoints,
    CloneInfo,
    CloneSpec,
    CloneStatus,
    LabActionHandle,
    LabError,
)
from faultline_product.adapters import (
    DEFAULT_RECIPES,
    FixtureCanaryDeployer,
    FixtureDevinAdapter,
    FixtureLeverAdapter,
    LabPatchVerifier,
)
from faultline_product.adapters.fixture import FixtureBrain, FixtureClock
from faultline_product.fixtures import load_fixture
from faultline_product.orchestrator import Orchestrator
from faultline_product.ports import (
    CanaryStatus,
    PatchProposal,
    PatchVerification,
    VerificationStatus,
)
from faultline_product.renderer import TerminalRenderer
from faultline_contracts import EventKind, JsonlSink, Stage

T0 = datetime(2026, 9, 19, 20, 0, tzinfo=timezone.utc)


class FakeLab:
    """In-memory CloneLab: records calls, never touches docker."""

    def __init__(self, *, create_error: Exception | None = None):
        self.created: list[CloneSpec] = []
        self.actions: list[tuple[str, str, dict, int]] = []
        self.undone: list[str] = []
        self.destroyed: list[str] = []
        self._create_error = create_error

    def create(self, spec):
        if self._create_error:
            raise self._create_error
        self.created.append(spec)
        return CloneInfo(
            clone_id=f"{spec.name}-1",
            status=CloneStatus.ready,
            spec=spec,
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
            ),
        )

    def apply(self, clone_id, action, params, ttl_s):
        self.actions.append((clone_id, action, params, ttl_s))
        return LabActionHandle(
            action_id=f"a{len(self.actions)}", clone_id=clone_id, action=action, params=params, ttl_s=ttl_s
        )

    def undo(self, handle):
        self.undone.append(handle.action_id)
        return handle

    def destroy(self, clone_id):
        self.destroyed.append(clone_id)


class FakeCloneTelemetry:
    """Serves fixture fingerprints, breached or healthy depending on the phase requested."""

    def __init__(self, bundle, *, heals: bool):
        series = bundle.telemetry.series(bundle.experiment_start - timedelta(seconds=60), bundle.telemetry.last_window_end)
        self._healthy = next(fp for fp in series if not any(s.breached for s in fp.slos))
        self._breached = next(fp for fp in series if any(s.breached for s in fp.slos))
        self._heals = heals
        self.started = self.stopped = False
        self._settle_at: datetime | None = None

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def series(self, start, end):
        n = max(1, int((end - start).total_seconds() // 5))
        # first call = replay window (incident visible), second = after settle
        if self._settle_at is None:
            self._settle_at = end
            return [self._mark(self._breached, "orders_v2")] * n
        fp = self._healthy if self._heals else self._breached
        return [self._mark(fp, "orders_v2")] * n

    @staticmethod
    def _mark(fp, service):
        return fp.model_copy(update={"services": {**fp.services, service: fp.services["orders"]}})


class RecordingLevers:
    def __init__(self):
        self.applied: list[tuple[str, dict, int]] = []

    def apply(self, lever_id, params, ttl_s):
        self.applied.append((lever_id, params, ttl_s))
        return ActionHandle(
            action_id="c1", lever_id=lever_id, params=params, applied_at=T0, ttl_s=ttl_s,
            undo=UndoSpec(lever_id=lever_id, payload={}),
        )


def _verifier(lab, telemetry, levers, context=Path("/tmp/patched")):
    clock = FixtureClock(T0)
    return LabPatchVerifier(
        lab,
        context=context,
        telemetry_factory=lambda clone, incident_id: telemetry,
        levers_factory=lambda clone: levers,
        sleep=clock.sleep,
        clock=clock,
        settle_s=10,
        healthy_windows=2,
    )


def _patch():
    return PatchProposal("fallback", "branch:faultline/fallback-retry-cap", "bounded retries")


def test_verifier_replays_recipe_on_v2_and_passes_when_clone_recovers():
    bundle = load_fixture("storm")
    lab, levers = FakeLab(), RecordingLevers()
    telemetry = FakeCloneTelemetry(bundle, heals=True)

    result = _verifier(lab, telemetry, levers).verify("inc-1", _patch(), "H_meta")

    assert result.status == VerificationStatus.passed
    assert lab.created[0].patch_ref == str(Path("/tmp/patched").resolve())
    assert lab.created[0].name.startswith("verify-inc-1")
    assert levers.applied[0][:2] == ("canary_weight", {"v2_weight": 1.0})
    assert lab.actions[0][1:] == ("db_latency", {"extra_ms": 800}, 20)
    assert result.recipe == DEFAULT_RECIPES["H_meta"]
    assert result.evidence["incident_reproduced"] is True
    assert result.evidence["breached_after_settle"] == 0
    assert lab.destroyed == [result.clone_id]
    assert telemetry.started and telemetry.stopped


def test_verifier_fails_when_incident_persists_after_trigger_ends():
    bundle = load_fixture("storm")
    lab = FakeLab()
    result = _verifier(lab, FakeCloneTelemetry(bundle, heals=False), RecordingLevers()).verify(
        "inc-2", _patch(), "H_db"
    )
    assert result.status == VerificationStatus.failed
    assert "still breached" in result.detail
    assert lab.actions[0][1] == "db_capacity"
    assert lab.destroyed  # clone torn down on failure too


def test_default_clone_telemetry_carries_writer_incident_and_clone_id():
    class Writer:
        def __init__(self):
            self.calls = []

        def write(self, fingerprint, *, incident_id=None, clone_id=None):
            self.calls.append((incident_id, clone_id))

    writer = Writer()
    lab = FakeLab()
    verifier = LabPatchVerifier(lab, context=Path("/tmp/patched"), writer=writer)
    clone = lab.create(CloneSpec(name="verify-x", patch_ref="/tmp/patched"))
    source = verifier._clone_telemetry(clone, "inc-9")
    assert source._writer is writer
    assert (source._incident_id, source._clone_id) == ("inc-9", clone.clone_id)
    assert source._optional_urls == {"orders_v2": "http://clone:9104/stats"}


def test_verifier_skips_without_lab_recipe_or_context():
    bundle = load_fixture("storm")
    unavailable = FakeLab(create_error=LabError("POST /clones -> 409: at capacity"))
    telemetry = FakeCloneTelemetry(bundle, heals=True)

    skipped = _verifier(unavailable, telemetry, RecordingLevers()).verify("inc", _patch(), "H_meta")
    assert skipped.status == VerificationStatus.skipped
    assert "clone lab unavailable" in skipped.detail

    no_recipe = _verifier(FakeLab(), telemetry, RecordingLevers()).verify("inc", _patch(), "H_other")
    assert no_recipe.status == VerificationStatus.skipped

    no_context = _verifier(FakeLab(), telemetry, RecordingLevers(), context=None).verify("inc", _patch(), "H_meta")
    assert no_context.status == VerificationStatus.skipped


class StubVerifier:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def verify(self, incident_id, patch, diagnosis, context=None):
        self.calls.append((incident_id, patch.reference, diagnosis))
        return self.result


def _run(tmp_path, verifier):
    bundle = load_fixture("storm")
    clock = FixtureClock(bundle.experiment_start, bundle.telemetry.last_window_end)
    audit = JsonlSink(tmp_path / "audit.jsonl")
    orchestrator = Orchestrator(
        FixtureLeverAdapter(clock=clock),
        audit,
        FixtureDevinAdapter(),
        FixtureCanaryDeployer(),
        TerminalRenderer(lambda _line: None),
        bundle.telemetry,
        FixtureBrain(bundle.triage, bundle.experiment, bundle.verdict),
        clock,
        clock.sleep,
        verifier=verifier,
    )
    return orchestrator.run("verify-run", bundle.experiment_start), audit.query("verify-run")


def test_failed_clone_verification_refuses_canary_and_pages_human(tmp_path):
    verifier = StubVerifier(
        PatchVerification(VerificationStatus.failed, "clone SLO still breached", clone_id="verify-1",
                          evidence={"breached_after_settle": 3})
    )
    result, events = _run(tmp_path, verifier)

    assert verifier.calls == [
        ("verify-run", "devin://task/verify-run", "H_meta"),
        ("verify-run", "devin://task/verify-run/rev1", "H_meta"),  # one Devin revision, re-verified
    ]
    assert result.patch.revision == 1
    assert result.verification.status == VerificationStatus.failed
    assert result.canary.status == CanaryStatus.refused
    assert "clone verification" in result.canary.detail
    assert not any(e.kind == EventKind.action_apply and e.stage == Stage.canary for e in events)
    refused = [e for e in events if e.kind == EventKind.refused and e.stage == Stage.patch]
    assert refused and refused[0].payload["evidence"] == {"breached_after_settle": 3}
    assert any(e.kind == EventKind.page_human and e.stage == Stage.patch for e in events)
    assert events[-1].payload["clone_verification"] == "failed"


def test_passed_clone_verification_proceeds_to_canary(tmp_path):
    result, events = _run(
        tmp_path, StubVerifier(PatchVerification(VerificationStatus.passed, "recovered", clone_id="verify-1"))
    )
    assert result.canary.status == CanaryStatus.passed
    assert any(e.kind == EventKind.action_apply and e.stage == Stage.canary for e in events)
    assert events[-1].payload["clone_verification"] == "passed"


class SequenceVerifier:
    """Returns one scripted result per call, keyed by patch revision."""

    def __init__(self, by_revision):
        self.by_revision = by_revision
        self.calls = []

    def verify(self, incident_id, patch, diagnosis, context=None):
        self.calls.append((patch.revision, context))
        return self.by_revision[patch.revision]


class UnrevisablePatches:
    def propose(self, incident_id, verdict, triage):
        return PatchProposal("fallback", "branch:faultline/fallback-retry-cap", "prebuilt")

    def revise(self, incident_id, patch, evidence):
        return None


class RecordingCheckout:
    def __init__(self, root: Path, fail_for=()):
        self.root, self.fail_for, self.calls = root, set(fail_for), []

    def resolve(self, patch):
        self.calls.append(patch.reference)
        if patch.reference in self.fail_for:
            from faultline_product.ports import CanaryPreparationError

            raise CanaryPreparationError("git fetch failed")
        return self.root / f"rev{patch.revision}"


def _run_with(tmp_path, *, verifier, patches=None, checkout=None, max_revisions=1, levers=None):
    bundle = load_fixture("storm")
    clock = FixtureClock(bundle.experiment_start, bundle.telemetry.last_window_end)
    audit = JsonlSink(tmp_path / "audit.jsonl")
    orchestrator = Orchestrator(
        levers or FixtureLeverAdapter(clock=clock),
        audit,
        patches or FixtureDevinAdapter(),
        FixtureCanaryDeployer(),
        TerminalRenderer(lambda _line: None),
        bundle.telemetry,
        FixtureBrain(bundle.triage, bundle.experiment, bundle.verdict),
        clock,
        clock.sleep,
        verifier=verifier,
        checkout=checkout,
        max_revisions=max_revisions,
    )
    return orchestrator.run("ship", bundle.experiment_start), audit.query("ship")


def test_failed_verification_is_sent_back_to_devin_and_revision_ships(tmp_path):
    failed = PatchVerification(VerificationStatus.failed, "still breached", evidence={"breached_after_settle": 3})
    passed = PatchVerification(VerificationStatus.passed, "recovered")
    verifier = SequenceVerifier({0: failed, 1: passed})
    checkout = RecordingCheckout(tmp_path)

    result, events = _run_with(tmp_path, verifier=verifier, checkout=checkout)

    assert verifier.calls == [(0, tmp_path / "rev0"), (1, tmp_path / "rev1")]
    assert checkout.calls == ["devin://task/ship", "devin://task/ship/rev1"]
    assert result.patch.revision == 1 and result.canary.status == CanaryStatus.passed
    opened = [e for e in events if e.kind == EventKind.patch_opened]
    assert [e.payload["revision"] for e in opened] == [0, 1]
    assert "still breached" in opened[1].payload["evidence_text"]
    assert '"breached_after_settle": 3' in opened[1].payload["evidence_text"]
    assert events[-1].payload["patch_reference"] == "devin://task/ship/rev1"


def test_max_revisions_bounds_the_loop(tmp_path):
    failed = PatchVerification(VerificationStatus.failed, "still breached")
    verifier = SequenceVerifier({0: failed, 1: failed, 2: failed})

    result, events = _run_with(tmp_path, verifier=verifier, max_revisions=2)

    assert [c[0] for c in verifier.calls] == [0, 1, 2]
    assert result.patch.revision == 2 and result.canary.status == CanaryStatus.refused
    assert sum(e.kind == EventKind.page_human for e in events) >= 1


def test_unrevisable_patch_pages_human_instead_of_looping(tmp_path):
    verifier = SequenceVerifier({0: PatchVerification(VerificationStatus.failed, "still breached")})

    result, events = _run_with(tmp_path, verifier=verifier, patches=UnrevisablePatches())

    assert [c[0] for c in verifier.calls] == [0]
    assert result.canary.status == CanaryStatus.refused
    paged = [e for e in events if e.kind == EventKind.page_human and "cannot be revised" in e.summary]
    assert paged and paged[0].payload["evidence_text"].startswith("clone verification failed")


def test_checkout_failure_is_recorded_and_flow_continues(tmp_path):
    verifier = SequenceVerifier({0: PatchVerification(VerificationStatus.skipped, "no checkout")})
    checkout = RecordingCheckout(tmp_path, fail_for={"devin://task/ship"})

    result, events = _run_with(tmp_path, verifier=verifier, checkout=checkout)

    assert verifier.calls == [(0, None)]
    refused = [e for e in events if e.kind == EventKind.refused and "could not check out" in e.summary]
    assert refused and refused[0].stage == Stage.patch
    assert result.canary.status == CanaryStatus.passed  # fixture deployer needs no checkout


def test_no_verifier_is_skipped_not_blocking(tmp_path):
    result, events = _run(tmp_path, None)
    assert result.verification.status == VerificationStatus.skipped
    assert result.canary.status == CanaryStatus.passed
