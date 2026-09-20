import json
import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from faultline_brain import DEFAULT_MODEL
from faultline_contracts import AuditSink, utcnow
from faultline_contracts.clone import CloneStatus, HttpCloneLab
from faultline_telemetry import JsonlFingerprintStore

from .adapters import (
    DEFAULT_RECIPES,
    ConcurrentPreparedVerifier,
    LabPatchVerifier,
    LiveTelemetrySource,
    PreparedCanaryDeployer,
    PreparedPatchAdapter,
    SandboxLeverAdapter,
    build_live_brain,
)
from .adapters.prepared import REPLAY_PROFILE
from .fixtures import load_fixture
from .orchestrator import Orchestrator
from .prepared_evidence import healthy_window_issue
from .renderer import TerminalRenderer

BASELINE_WINDOWS_S = 120
BASELINE_MIN_WINDOWS = 24
DETECT_SUSTAIN_S = 60


class _PreparedCheckout:
    def __init__(self, patches: PreparedPatchAdapter):
        self._patches = patches

    def resolve(self, patch) -> Path | None:
        self._patches.validate()
        return self._patches.context


def _write_ready(path: Path, payload: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
        os.link(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def run_prepared_watch(args, audit: AuditSink) -> int:
    incident_id = args.incident
    if audit.query(incident_id):
        print("faultline: error: incident already exists; pick a new id")
        return 2
    ready_file = Path(args.ready_file).expanduser().resolve()
    if ready_file.exists():
        print(f"faultline: error: ready file already exists: {ready_file}")
        return 2
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("faultline: error: prepared-watch requires OPENAI_API_KEY (no triage fallback in this profile)")
        return 2
    patches = PreparedPatchAdapter(Path(args.patch_context))
    try:
        patches.validate()
    except ValueError as exc:
        print(f"faultline: error: {exc}")
        return 2
    bundle = load_fixture("storm")
    try:
        brain = build_live_brain(
            bundle.experiments,
            api_key=api_key,
            model=getattr(args, "openai_model", DEFAULT_MODEL),
            triage_fallback=None,
        )
    except RuntimeError as exc:
        print(f"faultline: error: {exc}")
        return 2
    lab = HttpCloneLab(args.lab_url)
    try:
        target = lab.get(args.target_clone_id)
    except Exception as exc:  # noqa: BLE001 - lab unreachable or refused
        print(f"faultline: error: cannot fetch target clone: {exc}")
        return 2
    if not target.spec.name.startswith("demo-"):
        print("faultline: error: target clone is not a dedicated demo-* target")
        return 2
    patch_ref = target.spec.patch_ref
    if patch_ref is None or Path(patch_ref).expanduser().resolve() != patches.context:
        print("faultline: error: target clone was not built from this prepared snapshot")
        return 2
    if target.status != CloneStatus.ready or target.endpoints is None:
        print(f"faultline: error: target clone is {target.status.value}, not ready")
        return 2
    stats = target.endpoints.stats_urls
    writer = JsonlFingerprintStore(ready_file.parent / "fingerprints.jsonl")
    telemetry = LiveTelemetrySource(
        orders_url=stats["orders"],
        payments_url=stats["payments"],
        loadgen_url=stats["loadgen"],
        orders_v2_url=stats.get("orders-v2"),
        writer=writer,
        incident_id=incident_id,
        clone_id=None,
    )
    levers = SandboxLeverAdapter(base_url=target.endpoints.control_url, clock=utcnow)
    concurrent = None
    try:
        if not telemetry.healthz():
            print("faultline: error: target clone /stats not reachable")
            return 2
        if not levers.healthz():
            print("faultline: error: target clone control service not reachable")
            return 2
        telemetry.start()
        print(f"[prepared] collecting {args.baseline_s:g}s healthy baseline on {target.clone_id}")
        time.sleep(args.baseline_s)
        now = utcnow()
        latest = telemetry.latest()
        issue = healthy_window_issue(
            telemetry.series(now - timedelta(seconds=BASELINE_WINDOWS_S), now),
            minimum_windows=BASELINE_MIN_WINDOWS,
            service="orders",
        )
        if latest is None or any(slo.breached for slo in latest.slos) or issue is not None:
            print(f"faultline: error: baseline not healthy: {issue or 'SLO breached or no telemetry'}")
            return 2
        try:
            _write_ready(ready_file, {
                "incident_id": incident_id,
                "target_clone_id": target.clone_id,
                "patch_reference": patches.proposal.reference,
                "prepared": True,
                "replay_profile": REPLAY_PROFILE,
            })
        except FileExistsError:
            print(f"faultline: error: ready file appeared during the run: {ready_file}")
            return 2
        print(f"[prepared] ready; waiting for a sustained breach (timeout {args.detect_timeout:g}s)")
        try:
            telemetry.wait_for_breach(args.detect_timeout, sustain_s=DETECT_SUSTAIN_S)
        except TimeoutError:
            print(f"faultline: error: no SLO breach observed within {args.detect_timeout:g}s")
            return 3
        concurrent = ConcurrentPreparedVerifier(
            LabPatchVerifier(
                lab,
                context=patches.context,
                recipes={"H_meta": DEFAULT_RECIPES["H_meta"]},
                recipe_store=None,
                writer=writer,
                require_complete_evidence=True,
            ),
            patches,
        )
        concurrent.start(incident_id)
        orchestrator = Orchestrator(
            levers=levers,
            audit=audit,
            patches=patches,
            canary_deployer=PreparedCanaryDeployer(patches, target, lab=lab),
            verifier=concurrent,
            checkout=_PreparedCheckout(patches),
            max_revisions=0,
            investigation=None,
            require_verification=True,
            renderer=TerminalRenderer(),
            telemetry=telemetry,
            brain=brain,
            clock=utcnow,
            sleep=time.sleep,
        )
        result = orchestrator.run(incident_id=incident_id, now=utcnow())
        verification = result.verification
        canary = result.canary
        if (
            result.diagnosis == "H_meta"
            and verification is not None and verification.status.value == "passed"
            and canary is not None and canary.status.value == "passed"
        ):
            return 0
        return 1
    finally:
        try:
            telemetry.stop()
        finally:
            if concurrent is not None:
                concurrent.close()
