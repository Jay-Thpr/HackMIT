import argparse
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from faultline_contracts import JsonlSink, utcnow

from .adapters import (
    DevinAdapter,
    FixtureBrain,
    FixtureClock,
    FixtureDevinAdapter,
    FixtureLeverAdapter,
    SandboxLeverAdapter,
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
    watch.add_argument("--control-url", default="http://localhost:9901")

    investigate = commands.add_parser("investigate", help="detect, triage, and plan")
    investigate.add_argument("--fixture", choices=("storm",), default="storm")
    investigate.add_argument("--incident", help="override the generated incident id")
    investigate.add_argument("--levers", choices=("fixture", "sandbox"), default="fixture")
    investigate.add_argument("--control-url", default="http://localhost:9901")

    experiment = commands.add_parser("experiment", help="run a fixture experiment and judge it")
    experiment.add_argument("--fixture", choices=("storm",), default="storm")
    experiment.add_argument("--incident", help="override the generated incident id")
    experiment.add_argument("--id", required=True)
    experiment.add_argument("--levers", choices=("fixture", "sandbox"), default="fixture")
    experiment.add_argument("--control-url", default="http://localhost:9901")

    report = commands.add_parser("report", help="render an incident from the C4 audit log")
    report.add_argument("--incident", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    audit = JsonlSink(args.audit_log)
    try:
        if args.command == "report":
            print(render_report(audit, args.incident))
            return 0

        bundle = load_fixture(args.fixture)
        incident_id = args.incident or _run_incident_id(bundle.triage.incident_id)
        if args.command in {"watch", "investigate", "experiment"} and audit.query(incident_id):
            print("faultline: error: incident already exists; pick a new id")
            return 2
        first_breach = bundle.telemetry.first_breach()
        clock = FixtureClock(first_breach.window_end, bundle.telemetry.last_window_end)
        sleep = time.sleep if getattr(args, "real_time", False) else clock.sleep
        if args.levers == "sandbox":
            levers = SandboxLeverAdapter(base_url=args.control_url, clock=utcnow)
            if not levers.healthz():
                print(
                    f"faultline: error: sandbox control service not reachable at {args.control_url} "
                    "(cd sandbox && docker compose up -d)"
                )
                return 2
        else:
            levers = FixtureLeverAdapter(clock=clock)
        brain = FixtureBrain(bundle.triage, bundle.experiment, bundle.verdict)
        renderer = TerminalRenderer()
        orchestrator = Orchestrator(
            levers=levers,
            audit=audit,
            patches=_patch_adapter(args),
            renderer=renderer,
            telemetry=bundle.telemetry,
            brain=brain,
            clock=clock,
            sleep=sleep,
        )
        if args.command == "investigate":
            fp = orchestrator.detect(incident_id, first_breach.window_end)
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
                incident_id, bundle.experiment, first_breach.window_end
            )
            verdict = orchestrator.judge(
                incident_id, bundle.triage, bundle.experiment, baseline, during, after_release
            )
            print(verdict.summary)
        else:
            orchestrator.run(incident_id=incident_id, now=first_breach.window_end)
        return 0
    except ValueError as exc:
        parser_error = str(exc)
        print(f"faultline: error: {parser_error}")
        return 2


def _run_incident_id(base: str) -> str:
    suffix = datetime.now(timezone.utc).strftime("%H%M%S")
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
