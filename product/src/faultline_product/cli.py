import argparse
from datetime import datetime, timezone
from pathlib import Path

from faultline_contracts import JsonlSink

from .adapters import FixtureDevinAdapter, FixtureLeverAdapter
from .fixtures import load_fixture
from .orchestrator import Orchestrator
from .paths import DEFAULT_AUDIT_LOG
from .renderer import TerminalRenderer
from .report import render_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="faultline", description="Faultline operator CLI")
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT_LOG)
    commands = parser.add_subparsers(dest="command", required=True)

    watch = commands.add_parser("watch", help="run the incident workflow")
    watch.add_argument("--fixture", choices=("storm",), default="storm")
    watch.add_argument("--incident", help="override the generated incident id")

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
        Orchestrator(
            levers=FixtureLeverAdapter(),
            audit=audit,
            patches=FixtureDevinAdapter(),
            renderer=TerminalRenderer(),
        ).run(
            incident_id=incident_id,
            fingerprint=bundle.telemetry.first_breach(),
            triage=bundle.triage,
            experiment=bundle.experiment,
            verdict=bundle.verdict,
        )
        return 0
    except ValueError as exc:
        parser_error = str(exc)
        print(f"faultline: error: {parser_error}")
        return 2


def _run_incident_id(base: str) -> str:
    suffix = datetime.now(timezone.utc).strftime("%H%M%S")
    return f"{base}-{suffix}"


if __name__ == "__main__":
    raise SystemExit(main())
