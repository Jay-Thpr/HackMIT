import argparse
import json
import logging
import os
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path

from faultline_brain import DEFAULT_MODEL
from faultline_contracts import (
    Actor,
    AuditEvent,
    EventKind,
    JsonlSink,
    LeverError,
    Stage,
    utcnow,
)
from faultline_telemetry import (
    ElasticsearchAuditSink,
    ElasticsearchFingerprintStore,
    ElasticsearchTelemetryAnalytics,
    HttpElasticsearchClient,
    client_from_env,
    ensure_index_templates,
    load_repo_dotenv,
)
from faultline_telemetry.evidence import ElasticsearchEvidenceReader

from .adapters import (
    CommandPager,
    ElasticSimilarIncidents,
    PagingAuditSink,
    TeeAuditSink,
    WebhookPager,
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
    parser.add_argument("--pager-webhook", help="POST every page_human here (env: FAULTLINE_PAGER_WEBHOOK)")
    parser.add_argument("--pager-command", help="run this shell command with the page as JSON on stdin (env: FAULTLINE_PAGER_COMMAND)")
    commands = parser.add_subparsers(dest="command", required=True)

    watch = commands.add_parser("watch", help="run the incident workflow")
    watch.add_argument("--fixture", choices=("storm",), default="storm")
    watch.add_argument("--incident", help="override the generated incident id")
    watch.add_argument("--real-time", action="store_true")
    watch.add_argument("--resume", action="store_true",
                       help="continue an incident already in the audit log: keep its action budget and release leftover levers")
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
    watch.add_argument("--reasoning-provider", choices=("direct", "agent-builder"), default="direct")
    watch.add_argument("--elastic-evidence", action="store_true", help="attach scoped primary-Elasticsearch evidence to Agent Builder proposals")
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
    investigate.add_argument("--reasoning-provider", choices=("direct", "agent-builder"), default="direct")
    investigate.add_argument("--elastic-evidence", action="store_true", help="attach scoped primary-Elasticsearch evidence to Agent Builder proposals")

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
    experiment.add_argument("--reasoning-provider", choices=("direct", "agent-builder"), default="direct")
    experiment.add_argument("--elastic-evidence", action="store_true", help="attach scoped primary-Elasticsearch evidence to Agent Builder proposals")

    report = commands.add_parser("report", help="render an incident from the C4 audit log")
    report.add_argument("--incident", required=True)
    report.add_argument("--elastic-evidence", action="store_true", help="append scoped primary-Elasticsearch evidence summary")
    report.add_argument("--explain", action="store_true", help="append an Agent Builder evidence explanation (requires --elastic-evidence)")
    report.add_argument("--elasticsearch-url", help="primary Elasticsearch endpoint for --elastic-evidence")
    report.add_argument("--elasticsearch-api-key", help="primary Elasticsearch API key (env: FAULTLINE_ELASTICSEARCH_API_KEY)")

    ui = commands.add_parser("ui", help="serve the read-only incident API and the built UI")
    ui.add_argument("--port", type=int, default=8010)
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument(
        "--extra-audit-log", type=Path, action="append", default=[],
        help="additional C4 JSONL files to expose (e.g. integration/runs/audit-*.jsonl)",
    )

    replay = commands.add_parser("replay", help="run the stored incident replay suite in one fresh clone")
    replay.add_argument("incident", help="incident id; stored recipes are replayed regardless of diagnosis")
    replay.add_argument("--lab-url", required=True, help="C6 clone manager URL")
    replay.add_argument("--patch-context", type=Path, required=True, help="patched checkout to build as orders-v2")
    replay.add_argument("--diagnosis", default="H_meta", help="current incident diagnosis, for its default recipe")
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
    reasoning_provider = getattr(args, "reasoning_provider", "direct")
    elastic_evidence = getattr(args, "elastic_evidence", False)
    brain_mode = getattr(args, "brain", None) or (
        "live" if getattr(args, "telemetry", "fixture") == "sandbox" else "fixture"
    )
    if reasoning_provider == "agent-builder" and brain_mode != "live":
        print("faultline: error: --reasoning-provider agent-builder requires --brain live")
        return 2
    if getattr(args, "explain", False) and not elastic_evidence:
        print("faultline: error: --explain requires --elastic-evidence")
        return 2
    if elastic_evidence:
        if args.command != "report" and reasoning_provider != "agent-builder":
            print("faultline: error: --elastic-evidence requires --reasoning-provider agent-builder")
            return 2
        if not elasticsearch_url:
            print("faultline: error: --elastic-evidence requires a primary Elasticsearch URL (FAULTLINE_ELASTICSEARCH_URL)")
            return 2
    es_client = None
    if elasticsearch_url and getattr(args, "telemetry", None) == "sandbox":
        es_client = _persistence_client(elasticsearch_url, elasticsearch_api_key)
        audit = TeeAuditSink(audit, ElasticsearchAuditSink(es_client), log=log,
                             clone_id=getattr(args, "clone_id", None))
    pager_webhook = args.pager_webhook or os.environ.get("FAULTLINE_PAGER_WEBHOOK")
    pager_command = args.pager_command or os.environ.get("FAULTLINE_PAGER_COMMAND")
    if pager_webhook:
        audit = PagingAuditSink(audit, WebhookPager(pager_webhook), log=log)
    elif pager_command:
        audit = PagingAuditSink(audit, CommandPager(shlex.split(pager_command)), log=log)
    live_telemetry = None
    writer = None
    evidence_client = None
    try:
        if args.command == "report":
            reader = None
            explanation_client = None
            if elastic_evidence:
                evidence_client = HttpElasticsearchClient(elasticsearch_url, api_key=elasticsearch_api_key)
                reader = ElasticsearchEvidenceReader(evidence_client)
            if getattr(args, "explain", False):
                from faultline_brain.agent_builder import AgentBuilderClient

                explanation_client = AgentBuilderClient.from_env(role="report")
            print(render_report(audit, args.incident, evidence_reader=reader, explanation_client=explanation_client))
            return 0
        if args.command == "ui":
            import uvicorn

            from .api import UI_DIST, build_store, create_app

            paths = [args.audit_log, *args.extra_audit_log]
            store = build_store()
            print(f"[ui] audit logs: {', '.join(str(p) for p in paths)}")
            print(f"[ui] elasticsearch readings: {'on' if store else 'off (set FAULTLINE_ELASTICSEARCH_URL)'}")
            print(f"[ui] built UI: {'served from ' + str(UI_DIST) if UI_DIST.exists() else 'not built (cd product/ui && npm run build) — API only'}")
            print(f"[ui] http://{args.host}:{args.port}/  ·  http://{args.host}:{args.port}/api/incidents")
            uvicorn.run(create_app(paths, store), host=args.host, port=args.port, log_level="warning")
            return 0
        if args.command == "replay":
            return _run_replay(args)

        if (args.telemetry == "sandbox") != (args.levers == "sandbox"):
            print("faultline: error: sandbox telemetry and levers must be selected together")
            return 2
        if args.telemetry == "sandbox" and brain_mode != "live":
            print("faultline: error: sandbox mode requires --brain live")
            return 2
        bundle = load_fixture(args.fixture)
        incident_id = args.incident or _run_incident_id(bundle.triage.incident_id)
        resume = getattr(args, "resume", False)
        if args.command in {"watch", "investigate", "experiment"} and audit.query(incident_id) and not resume:
            print("faultline: error: incident already exists; pick a new id (or watch --resume)")
            return 2
        if resume and not audit.query(incident_id):
            print(f"faultline: error: nothing to resume; no audit events for {incident_id!r}")
            return 2
        proposal_client = None
        provider_sink = None
        investigator_client = None
        investigator_sink = None
        if reasoning_provider == "agent-builder":
            provider_sink = _reasoning_sink(audit, incident_id, Stage.triage)
            proposal_client = _proposal_client(args, "triage", provider_sink)
            investigator_sink = _reasoning_sink(audit, incident_id, Stage.experiment)
            investigator_client = _proposal_client(args, "investigator", investigator_sink)
        evidence_reader = None
        if elastic_evidence:
            if es_client is not None:
                evidence_reader = ElasticsearchEvidenceReader(es_client)
            else:
                evidence_client = HttpElasticsearchClient(elasticsearch_url, api_key=elasticsearch_api_key)
                evidence_reader = ElasticsearchEvidenceReader(evidence_client)
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
                proposal_client=proposal_client,
                provider_sink=provider_sink,
                evidence_reader=evidence_reader,
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
            investigation=_investigation(
                args,
                writer,
                proposal_client=investigator_client,
                provider_sink=investigator_sink,
                evidence_reader=evidence_reader,
            ),
            investigation_gate=getattr(args, "investigate_gate", False),
            similar=ElasticSimilarIncidents(ElasticsearchTelemetryAnalytics(es_client)) if es_client else None,
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
            if resume:
                orchestrator.resume(incident_id)
            orchestrator.run(incident_id=incident_id, now=now)
        return 0
    except (CanaryPreparationError, LeverError, RuntimeError, TelemetryUnavailable, ValueError) as exc:
        parser_error = str(exc)
        print(f"faultline: error: {parser_error}")
        return 2
    finally:
        try:
            if live_telemetry is not None:
                live_telemetry.stop()
        finally:
            if evidence_client is not None:
                evidence_client.close()
            if es_client is not None:
                es_client.close()


def _persistence_client(url, api_key):
    env = {**os.environ, "FAULTLINE_ELASTICSEARCH_URL": url,
           "FAULTLINE_ELASTICSEARCH_API_KEY": api_key or ""}
    client = client_from_env(env)
    if client is None:
        return None
    try:
        ensure_index_templates(getattr(client, "primary", client))
    except Exception as exc:  # noqa: BLE001 - ES persistence is optional
        log.warning("could not ensure Elasticsearch index templates (%s)", type(exc).__name__)
    return client


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


def _reasoning_sink(audit, incident_id, stage):
    def sink(metadata: dict) -> None:
        audit.write(
            AuditEvent(
                incident_id=incident_id,
                stage=stage,
                kind=EventKind.triage,
                actor=Actor.llm,
                summary="reasoning provider update",
                payload={"reasoning_provider": True, **metadata},
            )
        )

    return sink


def _proposal_client(args, role: str, provenance_sink=None):
    if getattr(args, "reasoning_provider", "direct") != "agent-builder":
        return None
    from faultline_brain.agent_builder import AgentBuilderClient

    return AgentBuilderClient.from_env(role=role, provenance_sink=provenance_sink)


def _investigation(args, writer=None, *, proposal_client=None, provider_sink=None, evidence_reader=None):
    if getattr(args, "no_investigate", False):
        return None
    if getattr(args, "levers", "fixture") != "sandbox":
        return FixtureInvestigation()
    lab_url = getattr(args, "lab_url", None)
    if not lab_url:
        return None
    from faultline_contracts.clone import HttpCloneLab

    agent = None
    agent_factory = None
    brain = getattr(args, "brain", None) or (
        "live" if getattr(args, "telemetry", "fixture") == "sandbox" else "fixture"
    )
    if brain == "live":
        api_key = os.environ.get("OPENAI_API_KEY")
        model = getattr(args, "openai_model", DEFAULT_MODEL)
        direct_agent = None
        if api_key:
            try:
                from openai import OpenAI
            except ImportError as exc:
                if proposal_client is None:
                    raise RuntimeError("install with: uv sync --extra llm") from exc
                if provider_sink:
                    provider_sink({"provider": "openai", "role": "investigator", "status": "unavailable", "reason": "ImportError"})
            else:
                from faultline_brain import InvestigatorAgent

                direct_agent = InvestigatorAgent(OpenAI(api_key=api_key), model=model)
        if proposal_client is not None:
            from faultline_brain import InvestigatorAgent
            from faultline_brain.agent_builder import FallbackInvestigatorAgent

            primary = InvestigatorAgent(proposal_client, model=model)
            agent = FallbackInvestigatorAgent(
                primary, direct_agent, provenance_sink=provider_sink
            )
            if evidence_reader is not None:
                from .adapters.evidence import EvidenceInvestigatorAgent

                def agent_factory(incident_id, hypothesis, clone, production_incident):
                    def sink(metadata):
                        if provider_sink:
                            provider_sink({**metadata, "hypothesis_id": hypothesis.id, "clone_id": clone.clone_id})
                    return FallbackInvestigatorAgent(
                        EvidenceInvestigatorAgent(
                            proposal_client, evidence_reader, incident_id, hypothesis.id, clone,
                            model=model, clock=utcnow, provenance_sink=sink,
                        ),
                        direct_agent,
                        provenance_sink=sink,
                    )
        elif direct_agent is not None:
            agent = direct_agent
        else:
            from faultline_brain import SeedInvestigator

            agent = SeedInvestigator()
    return LabInvestigation(
        HttpCloneLab(lab_url),
        writer=writer,
        max_clones=getattr(args, "max_clones", 1),
        agent=agent,
        budget=getattr(args, "investigate_budget", 3),
        agent_factory=agent_factory,
    )


def _patch_verifier(args, writer=None):
    if getattr(args, "levers", "fixture") != "sandbox":
        return FixturePatchVerifier()
    lab_url = getattr(args, "lab_url", None)
    if not lab_url:
        return None
    from faultline_contracts.clone import HttpCloneLab

    return LabPatchVerifier(HttpCloneLab(lab_url), writer=writer)


def _run_replay(args) -> int:
    """Manual, clone-only entry point for attaching the regression check to a patch PR."""
    from faultline_contracts.clone import HttpCloneLab

    verifier = LabPatchVerifier(HttpCloneLab(args.lab_url), context=args.patch_context)
    result = verifier.verify(
        args.incident,
        PatchProposal("replay", f"local:{args.patch_context}", "manual replay-suite run"),
        args.diagnosis,
        args.patch_context,
    )
    print(json.dumps({
        "incident_id": args.incident,
        "status": result.status.value,
        "detail": result.detail,
        "clone_id": result.clone_id,
        "evidence": result.evidence,
    }, default=str))
    return 0 if result.status.value == "passed" else 1


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
