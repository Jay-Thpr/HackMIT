from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from faultline_contracts import AuditEvent, EventKind, Stage
from faultline_contracts.clone import CloneSpec, CloneStatus, HttpCloneLab, LabError

from demo import load_env_file

ROOT = Path(__file__).resolve().parents[1]
LAB_URL = "http://127.0.0.1:19910"
BUDGET_S = 300.0


def qualify(events: list[AuditEvent], *, incident_id: str, elapsed_s: float,
            returncode: int | None, timed_out: bool) -> dict:
    relevant = [event for event in events if event.incident_id == incident_id]
    reports = [event for event in relevant if event.kind == EventKind.report]
    report = max(reports, key=lambda event: event.ts) if reports else None
    verdicts = [event for event in relevant if event.kind == EventKind.verdict]
    confirmed = any(event.payload.get("diagnosis") == "H_meta" and event.payload.get("confirmed") is True
                    for event in verdicts)
    prepared = any(event.kind == EventKind.patch_opened and event.payload.get("provider") == "prepared"
                   and str(event.payload.get("reference", "")).startswith("prepared:sha256:")
                   for event in relevant)
    payload = report.payload if report else {}
    replay_passed = payload.get("clone_verification") == "passed"
    canary_passed = payload.get("canary_status") == "passed"
    canaries = [event for event in relevant if event.kind == EventKind.action_apply
                and event.stage == Stage.canary and event.payload.get("lever_id") == "canary_weight"
                and event.payload.get("params", {}).get("v2_weight") == 0.05 and event.action_id]
    releases = [event for event in relevant if event.kind == EventKind.action_undo
                and event.payload.get("status") in ("undone", "expired")]
    complete_canary = any(
        end.action_id == start.action_id and (end.ts - start.ts).total_seconds() >= 120
        for start in canaries for end in releases
    )
    canary_ids = {event.action_id for event in canaries}
    checks = [event for event in relevant if event.stage == Stage.canary
              and event.kind in (EventKind.canary_update, EventKind.refused)
              and event.action_id in canary_ids]
    latest_check = max(checks, key=lambda event: event.ts) if checks else None
    split = latest_check.payload.get("evidence", {}) if latest_check else {}
    split_confirmed = (split.get("canary_split_status") == "compatible"
                       and split.get("canary_expected_share") == 0.05)
    finished = (returncode == 0 and report is not None and confirmed and prepared
                and payload.get("diagnosis") == "H_meta" and replay_passed and canary_passed
                and complete_canary and split_confirmed)
    within_budget = math.isfinite(elapsed_s) and 0 <= elapsed_s < BUDGET_S
    status = "timeout" if timed_out or not within_budget else ("passed" if finished else "failed")
    return {
        "schema_version": "faultline-fast-demo/1",
        "incident_id": incident_id,
        "status": status,
        "source": "live",
        "patch_source": "prepared",
        "replay_profile": "retry-storm-v1",
        "elapsed_s": elapsed_s,
        "budget_s": BUDGET_S,
        "within_budget": within_budget,
        "confirmed": confirmed,
        "prepared_patch_recorded": prepared,
        "clone_verification": payload.get("clone_verification", "unknown"),
        "canary_status": payload.get("canary_status", "unknown"),
        "canary_observed_and_released": complete_canary,
        "canary_split_status": split.get("canary_split_status", "unknown"),
        "canary_observed_share_estimate": split.get("canary_observed_share_estimate"),
        "canary_total_request_estimate": split.get("canary_total_request_estimate"),
        "canary_split_source": split.get("canary_split_source"),
        "process_returncode": returncode,
        "timing_boundary": "Before injection request through rendered final report; preparation and healthy baseline excluded",
        "qualification": "Single live rehearsal, not a reliability or generalization benchmark",
    }


def read_events(path: Path) -> list[AuditEvent]:
    if not path.exists():
        return []
    return [AuditEvent.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def isolated_lab(url: str) -> None:
    response = httpx.get(f"{url.rstrip('/')}/healthz", timeout=5)
    response.raise_for_status()
    metadata = response.json()
    if not str(metadata.get("project_prefix", "")).startswith("faultline-demo-"):
        raise ValueError("refusing a lab without a dedicated faultline-demo- project prefix")
    offset = metadata.get("port_offset")
    if not isinstance(offset, int) or offset < 20000:
        raise ValueError("refusing a lab without a dedicated port offset of at least 20000")


def wait_ready(path: Path, proc: subprocess.Popen, incident_id: str, target_id: str,
               timeout_s: float) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"prepared-watch exited before readiness ({proc.returncode})")
        if path.exists():
            ready = json.loads(path.read_text())
            if (ready.get("incident_id") != incident_id or ready.get("target_clone_id") != target_id
                    or ready.get("prepared") is not True or ready.get("replay_profile") != "retry-storm-v1"):
                raise ValueError("readiness marker does not identify this prepared run")
            return ready
        time.sleep(0.25)
    raise TimeoutError("preparation did not become ready; no incident was injected")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated live retry-storm demo with prepared patch and measured canary")
    parser.add_argument("--patch-context", type=Path, required=True)
    parser.add_argument("--lab-url", default=LAB_URL)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "runs")
    parser.add_argument("--env-file", type=Path, default=Path("~/.config/faultline/env"))
    parser.add_argument("--preparation-timeout-s", type=float, default=600)
    args = parser.parse_args(argv)
    if not math.isfinite(args.preparation_timeout_s) or args.preparation_timeout_s <= 0:
        parser.error("preparation timeout must be finite and positive")
    env = {**os.environ, **load_env_file(args.env_file.expanduser())}
    if not env.get("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is required; this live profile never substitutes fixture triage")
    patch_context = args.patch_context.expanduser().resolve()
    if not (patch_context / "prepared-patch.json").is_file():
        parser.error("patch-context must be a prepared snapshot from sandbox/scripts/prepare_demo_patch.py")
    try:
        isolated_lab(args.lab_url)
    except (httpx.HTTPError, ValueError) as exc:
        parser.error(f"dedicated demo lab is unavailable or unsafe ({type(exc).__name__})")
    incident_id = f"fast-{uuid.uuid4().hex[:12]}"
    output = args.output.expanduser().resolve() / incident_id
    output.mkdir(parents=True, exist_ok=False)
    audit = output / "audit.jsonl"
    ready_path = output / "ready.json"
    lab = HttpCloneLab(args.lab_url, timeout_s=240)
    existing_ids = {clone.clone_id for clone in lab.list()}
    target = None
    proc = None
    result = None
    started = None
    log_handle = None
    cleanup_errors: list[str] = []
    try:
        target = lab.create(CloneSpec(name=f"demo-{incident_id}", patch_ref=str(patch_context)))
        if target.status != CloneStatus.ready or target.endpoints is None:
            raise RuntimeError(f"isolated target is not ready: {target.detail}")
        command = ["uv", "run", "--extra", "llm", "faultline", "--audit-log", str(audit), "prepared-watch",
                   "--incident", incident_id, "--lab-url", args.lab_url,
                   "--target-clone-id", target.clone_id, "--patch-context", str(patch_context),
                   "--ready-file", str(ready_path)]
        for key in ("FAULTLINE_PAGER_WEBHOOK", "FAULTLINE_PAGER_COMMAND"):
            env.pop(key, None)
        env["PYTHONUNBUFFERED"] = "1"
        log_handle = (output / "cli.log").open("x")
        proc = subprocess.Popen(command, cwd=ROOT / "product", env=env,
                                stdout=log_handle, stderr=subprocess.STDOUT)
        ready = wait_ready(ready_path, proc, incident_id, target.clone_id, args.preparation_timeout_s)
        print(f"Prepared patch: {ready.get('patch_reference')} (not generated during this run)", flush=True)
        print(f"Healthy baseline ready. Evidence: {output}", flush=True)
        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        lab.apply(target.clone_id, "db_latency", {"extra_ms": 800}, ttl_s=20)
        print("Timer running: live incident, OpenAI triage, measured probe, replay and canary.", flush=True)
        timed_out = False
        remaining = BUDGET_S - (time.monotonic() - started)
        try:
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, BUDGET_S)
            proc.wait(timeout=remaining)
            remaining = BUDGET_S - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, BUDGET_S)
            report = subprocess.run(
                ["uv", "run", "faultline", "--audit-log", str(audit), "report", "--incident", incident_id],
                cwd=ROOT / "product", env=env, capture_output=True, text=True, timeout=remaining,
            )
            (output / "report.txt").write_text(report.stdout or report.stderr)
            print(report.stdout or report.stderr, flush=True)
            report_ok = report.returncode == 0
        except subprocess.TimeoutExpired:
            timed_out = True
            report_ok = False
        elapsed = time.monotonic() - started
        result = qualify(read_events(audit), incident_id=incident_id, elapsed_s=elapsed,
                         returncode=proc.poll() if report_ok else None, timed_out=timed_out)
        result["started_at"] = started_at.isoformat()
        result["patch_reference"] = ready.get("patch_reference")
    except (OSError, ValueError, RuntimeError, TimeoutError, httpx.HTTPError, LabError) as exc:
        result = {
            "schema_version": "faultline-fast-demo/1", "incident_id": incident_id,
            "status": "blocked" if started is None else "failed", "source": "live",
            "patch_source": "prepared", "detail": str(exc),
            "elapsed_s": None if started is None else time.monotonic() - started,
            "budget_s": BUDGET_S,
        }
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        if log_handle is not None:
            log_handle.close()
        owned = {target.clone_id} if target is not None else set()
        try:
            owned.update(clone.clone_id for clone in lab.list()
                         if clone.clone_id not in existing_ids
                         and clone.spec.name in {f"verify-{incident_id}", f"demo-{incident_id}"})
        except Exception as exc:
            cleanup_errors.append(f"could not inspect replay cleanup: {type(exc).__name__}")
        for clone_id in sorted(owned):
            try:
                lab.destroy(clone_id)
            except Exception as exc:
                cleanup_errors.append(f"{clone_id}: {type(exc).__name__}")
        if result is not None:
            result["cleanup_errors"] = cleanup_errors
            if cleanup_errors and result["status"] == "passed":
                result["status"] = "cleanup_failed"
            (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result is not None and result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
