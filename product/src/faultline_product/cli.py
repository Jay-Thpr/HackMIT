import argparse
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from faultline_brain import DEFAULT_MODEL
from faultline_contracts import JsonlSink, utcnow

from .adapters import (
    DevinAdapter,
    FixtureBrain,
    FixtureClock,
    FixtureDevinAdapter,
    FixtureLeverAdapter,
    LiveTelemetrySource,
    SandboxLeverAdapter,
    build_live_brain,
)
from .fixtures import load_fixture
from .orchestrator import Orchestrator
from .paths import DEFAULT_AUDIT_LOG
from .ports import PatchProposal
from .renderer import TerminalRenderer
from .report import render_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="faultline", description="Faultline operator CLI")
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT_LOG)
    commands = parser.add_subparsers(dest="command", required=True)

    watch = commands.add_parser("watch", help="run the incident workflow")
    watch.add_argument("--fixture", choices=("storm",), default="storm")
    watch.add_argument("--incident", help="override the generated incident id")
    watch.add_argument("--real-time", action="store_true")
    watch.add_argument("--devin", action="store_true")
    watch.add_argument("--levers", choices=("fixture", "sandbox"), default="fixture")
    watch.add_argument("--control-url")
    watch.add_argument("--telemetry", choices=("fixture", "sandbox"), default="fixture")
    watch.add_argument("--sandbox-host", default="127.0.0.1")
    watch.add_argument("--detect-timeout", type=float, default=300)
    watch.add_argument("--brain", choices=("fixture", "live"), default="fixture")
    watch.add_argument("--openai-model", default=DEFAULT_MODEL)

    investigate = commands.add_parser("investigate", help="detect, triage, and plan")
    investigate.add_argument("--fixture", choices=("storm",), default="storm")
    investigate.add_argument("--incident", help="override the generated incident id")
    investigate.add_argument("--levers", choices=("fixture", "sandbox"), default="fixture")
    investigate.add_argument("--control-url")
    investigate.add_argument("--telemetry", choices=("fixture", "sandbox"), default="fixture")
    investigate.add_argument("--sandbox-host", default="127.0.0.1")
    investigate.add_argument("--detect-timeout", type=float, default=300)
    investigate.add_argument("--brain", choices=("fixture", "live"), default="fixture")
    investigate.add_argument("--openai-model", default=DEFAULT_MODEL)

    experiment = commands.add_parser("experiment", help="run a fixture experiment and judge it")
    experiment.add_argument("--fixture", choices=("storm",), default="storm")
    experiment.add_argument("--incident", help="override the generated incident id")
    experiment.add_argument("--id", required=True)
    experiment.add_argument("--levers", choices=("fixture", "sandbox"), default="fixture")
    experiment.add_argument("--control-url")
    experiment.add_argument("--telemetry", choices=("fixture", "sandbox"), default="fixture")
    experiment.add_argument("--sandbox-host", default="127.0.0.1")
    experiment.add_argument("--detect-timeout", type=float, default=300)
    experiment.add_argument("--brain", choices=("fixture", "live"), default="fixture")
    experiment.add_argument("--openai-model", default=DEFAULT_MODEL)

    report = commands.add_parser("report", help="render an incident from the C4 audit log")
    report.add_argument("--incident", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    audit = JsonlSink(args.audit_log)
    live_telemetry = None
    try:
        if args.command == "report":
            print(render_report(audit, args.incident))
            return 0

        if args.telemetry == "sandbox" and args.levers != "sandbox":
            print("faultline: error: --telemetry sandbox requires --levers sandbox")
            return 2
        bundle = load_fixture(args.fixture)
        incident_id = args.incident or _run_incident_id(bundle.triage.incident_id)
        if args.command in {"watch", "investigate", "experiment"} and audit.query(incident_id):
            print("faultline: error: incident already exists; pick a new id")
            return 2
        if args.telemetry == "sandbox":
            host = args.sandbox_host
            live_telemetry = LiveTelemetrySource(
                orders_url=f"http://{host}:8101",
                payments_url=f"http://{host}:8102",
                loadgen_url=f"http://{host}:8103",
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
            print(f"[detect] waiting for checkout SLO breach (timeout {args.detect_timeout:g}s)")
            try:
                live_telemetry.wait_for_breach(args.detect_timeout)
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
        if args.brain == "live":
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
    except ValueError as exc:
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
    if getattr(args, "devin", False):
        return DevinAdapter(
            api_key=os.getenv("DEVIN_API_KEY"),
            repo="github.com/Jay-Thpr/HackMIT",
            fallback=fallback,
        )
    return FixtureDevinAdapter()


if __name__ == "__main__":
    raise SystemExit(main())
