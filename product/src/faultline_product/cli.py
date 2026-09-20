import argparse
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from faultline_brain import DEFAULT_MODEL
from faultline_contracts import JsonlSink, LeverError, utcnow
from faultline_telemetry import (
    ElasticsearchAuditSink,
    ElasticsearchFingerprintStore,
    HttpElasticsearchClient,
    ensure_index_templates,
    load_repo_dotenv,
)

from .adapters import (
    TeeAuditSink,
    DevinAdapter,
    CanaryPreparationError,
    FixtureCanaryDeployer,
    FixtureBrain,
    FixtureClock,
    FixtureDevinAdapter,
    FixtureInvestigation,
    FixturePatchCheckout,
    FixturePatchVerifier,
    GitPatchCheckout,
    LabInvestigation,
    LabPatchVerifier,
    FixtureLeverAdapter,
    LiveTelemetrySource,
    SandboxLeverAdapter,
    SandboxCanaryDeployer,
    TelemetryUnavailable,
    build_live_brain,
)
from .fixtures import load_fixture
from .orchestrator import Orchestrator
from .paths import DEFAULT_AUDIT_LOG, REPOSITORY_ROOT
from .ports import PatchProposal
from .renderer import TerminalRenderer
from .report import render_report

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="faultline", description="Faultline operator CLI")
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT_LOG)
    commands = parser.add_subparsers(dest="command", required=True)

    watch = commands.add_parser("watch", help="run the incident workflow")
    watch.add_argument("--fixture", choices=("storm",), default="storm")
    watch.add_argument("--incident", help="override the generated incident id")
    watch.add_argument("--real-time", action="store_true")
    watch.add_argument("--devin", action="store_true", help="ask Devin for the durable fix (needs DEVIN_API_KEY + DEVIN_ORG_ID)")
    watch.add_argument("--devin-acu-limit", type=int, default=5, help="max ACUs a Faultline-created Devin session may spend")
    watch.add_argument("--levers", choices=("fixture", "sandbox"), default="fixture")
    watch.add_argument("--control-url")
    watch.add_argument("--telemetry", choices=("fixture", "sandbox"), default="fixture")
    watch.add_argument("--sandbox-host", default="127.0.0.1")
    watch.add_argument("--elasticsearch-url", help="persist sandbox C1 windows to this Elasticsearch endpoint")
    watch.add_argument("--elasticsearch-api-key", help="Elastic Cloud API key (env: FAULTLINE_ELASTICSEARCH_API_KEY)")
    watch.add_argument("--clone-id", help="tag sandbox telemetry as this C6 clone in Elasticsearch")
    watch.add_argument("--detect-timeout", type=float, default=300)
    watch.add_argument(
        "--detect-sustain",
        type=float,
        default=60,
        help="seconds the SLO must stay breached before acting (PRD detector: 60)",
    )
    watch.add_argument("--brain", choices=("fixture", "live"))
    watch.add_argument("--openai-model", default=DEFAULT_MODEL)
    watch.add_argument(
        "--canary-context",
        type=Path,
        help="override: build this checkout as orders-v2 instead of resolving the patch reference",
    )
    watch.add_argument(
        "--max-revisions",
        type=int,
        default=1,
        help="how many times measured failures are sent back to Devin for a revised patch",
    )
    watch.add_argument(
        "--max-clones",
        type=int,
        default=1,
        help="investigation clones to run concurrently (2 + production caused Docker API 500s on a MacBook)",
    )
    watch.add_argument(
        "--no-investigate",
        action="store_true",
        help="skip stage 4a (clone investigators) even when --lab-url is set",
    )
    watch.add_argument(
        "--investigate-budget",
        type=int,
        default=3,
        help="lab-action proposals each clone investigator agent may try per hypothesis",
    )
    watch.add_argument(
        "--investigate-gate",
        action="store_true",
        help="drop hypotheses that fail to reproduce in a clone before the production probe",
    )
    watch.add_argument(
        "--lab-url",
        help="C6 clone manager (e.g. http://127.0.0.1:9910): replay the incident against the "
        "patch in a clean clone before the production canary",
    )

    investigate = commands.add_parser("investigate", help="detect, triage, and plan")
    investigate.add_argument("--fixture", choices=("storm",), default="storm")
    investigate.add_argument("--incident", help="override the generated incident id")
    investigate.add_argument("--levers", choices=("fixture", "sandbox"), default="fixture")
    investigate.add_argument("--control-url")
    investigate.add_argument("--telemetry", choices=("fixture", "sandbox"), default="fixture")
    investigate.add_argument("--sandbox-host", default="127.0.0.1")
    investigate.add_argument("--elasticsearch-url", help="persist sandbox C1 windows to this Elasticsearch endpoint")
    investigate.add_argument("--elasticsearch-api-key", help="Elastic Cloud API key (env: FAULTLINE_ELASTICSEARCH_API_KEY)")
    investigate.add_argument("--clone-id", help="tag sandbox telemetry as this C6 clone in Elasticsearch")
    investigate.add_argument("--detect-timeout", type=float, default=300)
    investigate.add_argument("--detect-sustain", type=float, default=60)
    investigate.add_argument("--brain", choices=("fixture", "live"))
    investigate.add_argument("--openai-model", default=DEFAULT_MODEL)

    experiment = commands.add_parser("experiment", help="run a fixture experiment and judge it")
    experiment.add_argument("--fixture", choices=("storm",), default="storm")
    experiment.add_argument("--incident", help="override the generated incident id")
    experiment.add_argument("--id", required=True)
    experiment.add_argument("--levers", choices=("fixture", "sandbox"), default="fixture")
    experiment.add_argument("--control-url")
    experiment.add_argument("--telemetry", choices=("fixture", "sandbox"), default="fixture")
    experiment.add_argument("--sandbox-host", default="127.0.0.1")
    experiment.add_argument("--elasticsearch-url", help="persist sandbox C1 windows to this Elasticsearch endpoint")
    experiment.add_argument("--elasticsearch-api-key", help="Elastic Cloud API key (env: FAULTLINE_ELASTICSEARCH_API_KEY)")
    experiment.add_argument("--clone-id", help="tag sandbox telemetry as this C6 clone in Elasticsearch")
    experiment.add_argument("--detect-timeout", type=float, default=300)
    experiment.add_argument("--detect-sustain", type=float, default=60)
    experiment.add_argument("--brain", choices=("fixture", "live"))
    experiment.add_argument("--openai-model", default=DEFAULT_MODEL)

    report = commands.add_parser("report", help="render an incident from the C4 audit log")
    report.add_argument("--incident", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_repo_dotenv(Path.cwd())
    args = build_parser().parse_args(argv)
    audit = JsonlSink(args.audit_log)
    elasticsearch_url = getattr(args, "elasticsearch_url", None) or os.environ.get(
        "FAULTLINE_ELASTICSEARCH_URL"
    )
    elasticsearch_api_key = getattr(args, "elasticsearch_api_key", None) or os.environ.get(
        "FAULTLINE_ELASTICSEARCH_API_KEY"
    )
    es_client = None
    if elasticsearch_url:
        es_client = HttpElasticsearchClient(elasticsearch_url, api_key=elasticsearch_api_key)
        try:
            ensure_index_templates(es_client)
        except Exception as exc:  # noqa: BLE001 - ES persistence is optional
            log.warning("could not ensure Elasticsearch index templates: %s", exc)
        audit = TeeAuditSink(audit, ElasticsearchAuditSink(es_client), log=log)
    live_telemetry = None
    writer = None
    try:
        if args.command == "report":
            print(render_report(audit, args.incident))
            return 0

        if (args.telemetry == "sandbox") != (args.levers == "sandbox"):
            print("faultline: error: sandbox telemetry and levers must be selected together")
            return 2
        brain_mode = args.brain or ("live" if args.telemetry == "sandbox" else "fixture")
        if args.telemetry == "sandbox" and brain_mode != "live":
            print("faultline: error: sandbox mode requires --brain live")
            return 2
        bundle = load_fixture(args.fixture)
        incident_id = args.incident or _run_incident_id(bundle.triage.incident_id)
        if args.command in {"watch", "investigate", "experiment"} and audit.query(incident_id):
            print("faultline: error: incident already exists; pick a new id")
            return 2
        if args.telemetry == "sandbox":
            host = args.sandbox_host
            writer = ElasticsearchFingerprintStore(es_client) if es_client else None
            live_telemetry = LiveTelemetrySource(
                orders_url=f"http://{host}:8101",
                payments_url=f"http://{host}:8102",
                loadgen_url=f"http://{host}:8103",
                orders_v2_url=f"http://{host}:8104",
                writer=writer,
                incident_id=incident_id,
                clone_id=args.clone_id,
            )
            if not live_telemetry.healthz():
                print(
                    "faultline: error: sandbox /stats not reachable "
                    "(cd sandbox && docker compose up -d)"
                )
                return 2
            live_telemetry.start()
            telemetry = live_telemetry
            clock = utcnow
            sleep = time.sleep
            print(
                f"[detect] waiting for checkout SLO breach sustained {args.detect_sustain:g}s "
                f"(timeout {args.detect_timeout:g}s)"
            )
            try:
                live_telemetry.wait_for_breach(args.detect_timeout, sustain_s=args.detect_sustain)
            except TimeoutError:
                print(f"faultline: error: no SLO breach observed within {args.detect_timeout:g}s")
                return 3
        else:
            telemetry = bundle.telemetry
            clock = FixtureClock(bundle.experiment_start, bundle.telemetry.last_window_end)
            use_real_time = getattr(args, "real_time", False)
            sleep = clock.real_sleep if use_real_time else clock.sleep
        if args.levers == "sandbox":
            control_url = args.control_url or f"http://{args.sandbox_host}:9901"
            levers = SandboxLeverAdapter(base_url=control_url, clock=utcnow)
            if not levers.healthz():
                print(
                    f"faultline: error: sandbox control service not reachable at {control_url} "
                    "(cd sandbox && docker compose up -d)"
                )
                return 2
        else:
            levers = FixtureLeverAdapter(clock=clock)
        if brain_mode == "live":
            brain = build_live_brain(
                bundle.experiments,
                api_key=os.environ.get("OPENAI_API_KEY"),
                model=args.openai_model,
                triage_fallback=bundle.triage,
            )
        else:
            brain = FixtureBrain(bundle.triage, bundle.experiment, bundle.verdict)
        renderer = TerminalRenderer()
        orchestrator = Orchestrator(
            levers=levers,
            audit=audit,
            patches=_patch_adapter(args),
            canary_deployer=_canary_deployer(args),
            verifier=_patch_verifier(args, writer),
            checkout=_patch_checkout(args),
            max_revisions=getattr(args, "max_revisions", 1),
            investigation=_investigation(args, writer),
            investigation_gate=getattr(args, "investigate_gate", False),
            renderer=renderer,
            telemetry=telemetry,
            brain=brain,
            clock=clock,
            sleep=sleep,
        )
        now = utcnow() if args.telemetry == "sandbox" else bundle.experiment_start
        if args.command == "investigate":
            fp = orchestrator.detect(incident_id, now)
            triage = orchestrator.triage(incident_id, fp)
            experiment = orchestrator.plan(incident_id, triage)
            for hypothesis in triage.hypotheses:
                print(f"{hypothesis.id}: {hypothesis.label} — {hypothesis.description}")
            if experiment is not None:
                print(f"Experiment: {experiment.id} ({experiment.lever_id} {experiment.params})")
                print(f"Blast radius: {experiment.blast_radius_pct:g}%")
        elif args.command == "experiment":
            if args.id != bundle.experiment.id:
                raise ValueError(f"unknown fixture experiment {args.id!r}")
            baseline, during, after_release = orchestrator.experiment(
                incident_id, bundle.experiment, now
            )
            verdict = orchestrator.judge(
                incident_id, bundle.triage, bundle.experiment, baseline, during, after_release
            )
            print(verdict.summary)
        else:
            orchestrator.run(incident_id=incident_id, now=now)
        return 0
    except (CanaryPreparationError, LeverError, RuntimeError, TelemetryUnavailable, ValueError) as exc:
        parser_error = str(exc)
        print(f"faultline: error: {parser_error}")
        return 2
    finally:
        if live_telemetry is not None:
            live_telemetry.stop()


def _run_incident_id(base: str) -> str:
    suffix = datetime.now(UTC).strftime("%H%M%S")
    return f"{base}-{suffix}"


def _patch_adapter(args):
    fallback = PatchProposal(
        provider="fallback",
        reference="branch:faultline/fallback-retry-cap",
        summary="Prebuilt patch: bounded retries with exponential backoff and jitter",
    )
    use_devin = getattr(args, "devin", False)
    if use_devin or getattr(args, "levers", "fixture") == "sandbox":
        adapter = DevinAdapter(
            api_key=os.getenv("DEVIN_API_KEY") if use_devin else None,
            org_id=os.getenv("DEVIN_ORG_ID") if use_devin else None,
            repo="github.com/Jay-Thpr/HackMIT",
            fallback=fallback,
            max_acu_limit=getattr(args, "devin_acu_limit", 5),
        )
        if use_devin and not adapter.enabled:
            print(
                "[patch] --devin set but DEVIN_API_KEY/DEVIN_ORG_ID missing "
                "(v3 needs a cog_ service-user key and the org id); using the fallback patch"
            )
        return adapter
    return FixtureDevinAdapter()


def _patch_checkout(args):
    if getattr(args, "levers", "fixture") != "sandbox":
        return FixturePatchCheckout()
    return GitPatchCheckout(
        repo_root=REPOSITORY_ROOT,
        workdir=REPOSITORY_ROOT / ".faultline" / "worktrees",
        override=getattr(args, "canary_context", None),
    )


def _investigation(args, writer=None):
    if getattr(args, "no_investigate", False):
        return None
    if getattr(args, "levers", "fixture") != "sandbox":
        return FixtureInvestigation()
    lab_url = getattr(args, "lab_url", None)
    if not lab_url:
        return None
    from faultline_contracts.clone import HttpCloneLab

    agent = None
    brain = getattr(args, "brain", None) or (
        "live" if getattr(args, "telemetry", "fixture") == "sandbox" else "fixture"
    )
    if brain == "live":
        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("install with: uv sync --extra llm") from exc
            from faultline_brain import InvestigatorAgent

            agent = InvestigatorAgent(
                OpenAI(api_key=api_key), model=getattr(args, "openai_model", DEFAULT_MODEL)
            )
        else:
            from faultline_brain import SeedInvestigator

            agent = SeedInvestigator()
    return LabInvestigation(
        HttpCloneLab(lab_url),
        writer=writer,
        max_clones=getattr(args, "max_clones", 1),
        agent=agent,
        budget=getattr(args, "investigate_budget", 3),
    )


def _patch_verifier(args, writer=None):
    if getattr(args, "levers", "fixture") != "sandbox":
        return FixturePatchVerifier()
    lab_url = getattr(args, "lab_url", None)
    if not lab_url:
        return None
    from faultline_contracts.clone import HttpCloneLab

    return LabPatchVerifier(HttpCloneLab(lab_url), writer=writer)


def _canary_deployer(args):
    if getattr(args, "levers", "fixture") == "sandbox":
        host = args.sandbox_host
        return SandboxCanaryDeployer(
            compose_dir=REPOSITORY_ROOT / "sandbox",
            orders_v2_url=f"http://{host}:8104",
        )
    return FixtureCanaryDeployer()


if __name__ == "__main__":
    raise SystemExit(main())
