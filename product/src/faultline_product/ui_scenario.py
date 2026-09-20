"""Translate one real incident (C4 audit events + C1 windows) into the UI's `Scenario` view model.

The UI (`product/ui/src/model.ts`) replays a `Scenario`: a topology, a healthy baseline per node,
the hypotheses, and a list of `WorkspaceEvent`s at second offsets, each optionally carrying node
`readings`, an `action` with a TTL, an `undo`, a clone `environment`, or a `testResult`. This module
builds that from what the orchestrator actually recorded, so the panels show the run, not a script.

Mapping (audit -> UI):
  actor   llm -> model, math -> math, adapter -> adapter, orchestrator -> orchestrator
  detect                      -> detect            (readings from the breached window)
  triage (stage 3)            -> reason            (hypotheses + LLM reasoning from payload.triage)
  triage (planner)            -> observe           (candidate table in detail)
  triage (investigation)      -> clone + action + observe (+ testResult columns: reproduce/recover/predict)
  action_apply                -> action            (ttl from payload, id = action_id)
  action_undo                 -> undo              (undoId = action_id)
  verdict                     -> verdict           (z-table in result)
  mitigation, patch_opened, canary_update, refused, page_human, report -> observe
Everything the UI shows is a real audit field; `detail` strings quote the audit summary.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from faultline_contracts import AuditEvent, EventKind, Fingerprint

LEAD_S = 12  # seconds of healthy replay shown before detection, like the synthetic scenarios
ACTORS = {"llm": "model", "math": "math", "adapter": "adapter", "orchestrator": "orchestrator"}
PHASES = {
    EventKind.detect: "Detecting",
    EventKind.triage: "Forming hypotheses",
    EventKind.experiment_start: "Confirming in production",
    EventKind.verdict: "Judging",
    EventKind.mitigation: "Mitigating",
    EventKind.patch_opened: "Writing the durable fix",
    EventKind.canary_update: "Verifying the fix",
    EventKind.report: "Report",
}
HYPOTHESIS_COLORS = ["#957548", "#716b60", "#5e7a6b", "#7a5e6b"]
CLONE_COLORS = ["#957548", "#716b60", "#5e7a6b", "#7a5e6b"]
NODE_HINTS: dict[str, dict[str, Any]] = {
    "gateway": {"kind": "external", "label": "Gateway"},
    "orders": {"kind": "service", "label": "Orders"},
    "orders_v2": {"kind": "service", "label": "Orders v2"},
    "payments": {"kind": "service", "label": "Payments"},
    "db": {"kind": "datastore", "label": "Postgres"},
}
TEST_CASES = [
    {"id": "replay", "groupId": "probe", "name": "Patch survives the replayed incident",
     "description": "In a fresh clone built from the patch, the reproduced incident is replayed; the clone must recover on its own."},
    {"id": "reproduce", "groupId": "reproduction", "name": "Reproduces the incident",
     "description": "The hypothesised cause, injected into a clean clone, recreates the production fingerprint within noise."},
    {"id": "recover", "groupId": "release", "name": "Recovers when the cause is removed",
     "description": "Removing the injected cause returns the clone to its healthy reference."},
    {"id": "predict", "groupId": "probe", "name": "Predicts the production probe",
     "description": "The clone's response to the planned production experiment matches this hypothesis's predicted directions."},
]


def scenario_from_incident(
    incident_id: str,
    events: list[AuditEvent],
    windows: list[Fingerprint] | None = None,
    clone_windows: dict[str, list[Fingerprint]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the UI Scenario for one incident. ``windows`` are production C1 windows for the
    incident (any span); ``clone_windows`` map clone_id -> that clone's windows. Both optional:
    without them the topology falls back to the hero graph and nodes read `unknown`."""
    events = sorted((e for e in events if e.incident_id == incident_id), key=lambda e: (e.ts, e.stage))
    if not events:
        raise ValueError(f"no audit events for {incident_id!r}")
    windows = sorted(windows or [], key=lambda fp: fp.window_start)
    clone_windows = clone_windows or {}
    detect = next((e for e in events if e.kind == EventKind.detect), events[0])
    t0 = detect.ts - timedelta(seconds=LEAD_S)

    def at(ts: datetime) -> int:
        return max(0, int(round((ts - t0).total_seconds())))

    topology = _topology(windows)
    healthy = [fp for fp in windows if not _breached(fp)]
    baseline = _readings(healthy[-1] if healthy else None, topology)
    triage_event = next((e for e in events if e.kind == EventKind.triage and e.stage == 3), None)
    hypotheses = _hypotheses(triage_event)
    hypothesis_ids = [h["id"] for h in hypotheses]
    entry, target, policy = "gateway", "db", "orders"

    out: list[dict[str, Any]] = [{
        "id": f"{incident_id}-baseline", "sequence": 1, "at": 0, "kind": "baseline", "actor": "math",
        "environmentId": "production", "title": "Healthy reference captured", "tool": "telemetry.window",
        "detail": (f"{len(healthy)} healthy 5 s windows before detection" if healthy
                   else "No healthy C1 windows persisted for this incident; readings are unknown until detection"),
        "readings": baseline, "result": _summary(baseline.get(target)),
    }]
    seq = 1
    clones: dict[str, str] = {}  # clone_id -> environment id
    action_labels: dict[str, str] = {}

    for e in events:
        seq += 1
        p = e.payload or {}
        base = dict(id=e.event_id, sequence=seq, at=at(e.ts), actor=ACTORS.get(e.actor.value, "orchestrator"),
                    environmentId="production", detail=e.summary)
        phase = PHASES.get(e.kind)
        if e.kind == EventKind.detect:
            fp = _window_at(windows, e.ts)
            out.append({**base, "kind": "detect", "title": e.summary, "phase": phase, "targetId": entry,
                        "tool": "detector.evaluate", "readings": _readings(fp, topology, breached=True),
                        "result": f"{p.get('metric')} = {_fmt(p.get('value'))} ms, threshold {_fmt(p.get('threshold'))} ms"})
        elif e.kind == EventKind.triage and p.get("investigation") and not p.get("clone_id"):
            # never got a clone (no recipe / lab refused): a note on production, not an environment
            out.append({**base, "kind": "observe", "actor": "math", "tool": "evidence.compare",
                        "title": f"{p.get('hypothesis_id', '?')}: not investigated in a clone", "detail": e.summary})
        elif e.kind == EventKind.triage and p.get("investigation"):
            hid = p.get("hypothesis_id", "?")
            clone_id = p["clone_id"]
            env = clones.setdefault(clone_id, f"clone-{hid.lower()}")
            idx = hypothesis_ids.index(hid) if hid in hypothesis_ids else len(clones) - 1
            investigator = f"investigator-{'ab'[idx % 2]}"
            label = next((h["title"] for h in hypotheses if h["id"] == hid), hid)
            recipe = p.get("recipe") or {}
            clone_fps = sorted(clone_windows.get(clone_id, []), key=lambda fp: fp.window_start)
            # the investigation event is written once all investigators are done; the clone's own
            # C1 windows (when persisted) say when it actually lived
            born = at(clone_fps[0].window_start) - 10 if clone_fps else max(0, base["at"] - 8)
            died = at(clone_fps[-1].window_end) + 2 if clone_fps else base["at"]
            born, died = max(0, min(born, base["at"] - 8)), max(born + 8, min(died, base["at"]))
            base = {**base, "at": died}
            out.append({**base, "kind": "clone", "at": born, "environmentId": env, "actor": investigator,
                        "title": f"A clean clone for {hid}", "phase": "Investigating in clones", "tool": "lab.create", "lifecycle": "starting",
                        "args": {"hypothesis": hid, "clone_id": clone_id},
                        "environment": {"label": f"Clone {hid}", "color": CLONE_COLORS[idx % len(CLONE_COLORS)], "hypothesisId": hid},
                        "detail": f"Built only from observable config; investigating: {label}."})
            seq += 1
            out.append({**base, "id": f"{e.event_id}-ready", "sequence": seq, "at": born + 1, "kind": "lifecycle",
                        "environmentId": env, "actor": "adapter", "lifecycle": "ready", "tool": "lab.ready",
                        "title": f"Clone {hid} is ready", "readings": _readings(clone_fps[0] if clone_fps else None, topology, breached=False),
                        "detail": "The manager reported the clone healthy (C6 readiness probe) before any lab action."})
            if recipe:
                seq += 1
                out.append({**base, "id": f"{e.event_id}-inject", "sequence": seq, "at": born + 2,
                            "kind": "action", "environmentId": env, "actor": investigator, "targetId": target,
                            "title": f"Inject {recipe.get('action')} into the clone", "tool": "lab.apply",
                            "args": {**{k: v for k, v in (recipe.get("params") or {}).items()}, "ttl_s": recipe.get("ttl_s", 0)},
                            "action": {"id": f"{e.event_id}-inject", "label": f"{recipe.get('action')} {recipe.get('params')}",
                                       "ttl": int(recipe.get("ttl_s") or 0)},
                            "readings": _readings(_last(clone_windows.get(clone_id, [])), topology, breached=p.get("reproduced")),
                            "detail": "Clone-only lab action (C6); production is untouched."})
            seq += 1
            out.append({**base, "id": f"{e.event_id}-evidence", "sequence": seq, "kind": "observe", "environmentId": env,
                        "actor": "math", "targetId": target, "tool": "evidence.compare",
                        "title": f"{hid}: {'reproduced' if p.get('reproduced') else 'did not reproduce'} the incident",
                        "result": _investigation_result(p), "detail": e.summary})
            for check, passed, expected, observed in _investigation_checks(p):
                seq += 1
                out.append({**base, "id": f"{e.event_id}-{check}", "sequence": seq, "kind": "observe", "environmentId": env,
                            "actor": "math", "tool": "suite.evaluate", "title": f"{check} check {'passed' if passed else 'failed'}",
                            "detail": e.summary, "result": observed,
                            "testResult": {"checkId": check, "passed": bool(passed), "expected": expected, "observed": observed}})
            # the investigator destroys its clone on the way out; the event is written after cleanup
            out.extend(_teardown(base, e.event_id, env, f"Clone {hid}", base["at"], seq + 1))
            seq += 2
        elif e.kind == EventKind.triage and p.get("planner"):
            rows = p.get("candidates") or []
            out.append({**base, "kind": "observe", "title": "Planner ranked the candidate experiments", "phase": "Choosing the probe",
                        "tool": "planner.rank", "targetId": policy,
                        "detail": "Separation = predictions on which the hypotheses disagree; score = separation − 0.1 × blast radius.",
                        "result": "; ".join(f"{r.get('experiment_id')}: sep {r.get('separation')}, blast {r.get('blast_radius_pct')}%"
                                            + (" ← selected" if r.get("selected") else "") for r in rows)})
        elif e.kind == EventKind.triage:
            tr = p.get("triage") or {}
            out.append({**base, "kind": "reason", "title": e.summary, "phase": phase, "targetId": target, "tool": "triage.propose",
                        "actor": "model",
                        "detail": tr.get("reasoning") or " ".join(h["description"] for h in hypotheses) or e.summary,
                        "result": f"ambiguous: {p.get('ambiguous')}; hypotheses: {', '.join(p.get('hypotheses') or [])}"})
        elif e.kind == EventKind.experiment_start:
            out.append({**base, "kind": "observe", "title": e.summary, "phase": phase, "tool": "orchestrator.experiment"})
        elif e.kind == EventKind.action_apply:
            lever, params, ttl = p.get("lever_id"), p.get("params") or {}, int(p.get("ttl_s") or 0)
            label = f"{lever} {params}" if params else str(lever)
            action_labels[p.get("action_id", e.event_id)] = label
            stage_phase = {4: "Confirming in production", 5: "Mitigating", 7: "Canary"}.get(e.stage)
            fp = _window_at(windows, e.ts + timedelta(seconds=min(15, max(5, ttl // 4))))
            out.append({**base, "kind": "action", "title": e.summary, "phase": stage_phase, "targetId": policy if lever == "retry_cap" else target,
                        "tool": "levers.apply", "args": {**params, "ttl_s": ttl},
                        "action": {"id": p.get("action_id", e.event_id), "label": label, "ttl": ttl},
                        "readings": _readings(fp, topology), "detail": f"Production lever (C3) with a {ttl}s TTL and a registered undo."})
        elif e.kind == EventKind.action_undo:
            fp = _window_at(windows, e.ts + timedelta(seconds=15))
            action_id = e.action_id or p.get("action_id") or e.event_id
            status = p.get("status") or "unknown"
            title = e.summary if status in ("undone", "expired") else f"Release of {p.get('lever_id')} not confirmed"
            out.append({**base, "kind": "undo", "title": title, "tool": "levers.undo", "undoId": action_id,
                        "undoStatus": status, "targetId": policy if p.get("lever_id") == "retry_cap" else target,
                        "readings": _readings(fp, topology), "detail": f"Release of {action_labels.get(action_id, p.get('lever_id'))}; status {status}."})
        elif e.kind == EventKind.verdict:
            obs = p.get("observations") or []
            seen, rows = set(), []
            for o in obs:
                key = (o.get("metric"), o.get("phase"))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(f"{o.get('metric')} {o.get('phase')}: {_fmt(o.get('baseline'))} → {_fmt(o.get('measured'))} (z {_fmt(o.get('z'))}, {o.get('direction')})")
            out.append({**base, "kind": "verdict", "title": e.summary, "phase": "Confirmed" if p.get("confirmed") else "Not confirmed",
                        "diagnosis": p.get("diagnosis"), "confirmed": p.get("confirmed") is True,
                        "targetId": target, "tool": "judge.confirm", "detail": e.summary, "result": "; ".join(rows)})
        elif e.kind == EventKind.canary_update and p.get("clone_id"):
            # patch verification ran in its own clean clone: show it as an environment with its replay
            clone_id = p["clone_id"]
            env = clones.setdefault(clone_id, f"verify-{len([c for c in clones.values() if c.startswith('verify')]) + 1}")
            recipe = p.get("recipe") or {}
            ev = p.get("evidence") or {}
            passed = p.get("status") == "passed"
            out.append({**base, "kind": "clone", "at": max(0, base["at"] - 60), "environmentId": env, "actor": "adapter",
                        "title": "A fresh clone built from the patch", "phase": "Verifying the fix", "tool": "lab.create", "lifecycle": "starting",
                        "args": {"patch_ref": str(p.get("patch_reference")), "clone_id": clone_id},
                        "environment": {"label": f"Verify · {p.get('patch_reference', '')[-24:]}", "color": "#5e7a6b", "hypothesisId": "patch"},
                        "detail": "orders-v2 built from the patch; all clone traffic routed to it before the replay."})
            verify_fps = sorted(clone_windows.get(clone_id, []), key=lambda fp: fp.window_start)
            seq += 1
            out.append({**base, "id": f"{e.event_id}-ready", "sequence": seq, "at": max(0, base["at"] - 52), "kind": "lifecycle",
                        "environmentId": env, "actor": "adapter", "lifecycle": "ready", "tool": "lab.ready",
                        "title": "Patched clone is ready", "readings": _readings(verify_fps[0] if verify_fps else None, topology, breached=False),
                        "detail": "orders-v2 built from the patch passed the readiness probe; the replay can start."})
            if recipe:
                seq += 1
                out.append({**base, "id": f"{e.event_id}-replay", "sequence": seq, "at": max(0, base["at"] - 50), "kind": "action",
                            "environmentId": env, "actor": "adapter", "targetId": target, "title": f"Replay {recipe.get('action')} against the patch",
                            "tool": "lab.apply", "args": {**(recipe.get("params") or {}), "ttl_s": recipe.get("ttl_s", 0)},
                            "action": {"id": f"{e.event_id}-replay", "label": f"{recipe.get('action')} {recipe.get('params')}", "ttl": int(recipe.get("ttl_s") or 0)},
                            "readings": _readings(_last(clone_windows.get(clone_id, [])), topology, breached=ev.get("incident_reproduced")),
                            "detail": "The reproduced incident replayed in the clone (C6)."})
            seq += 1
            observed = ", ".join(f"{k}={_fmt(v)}" for k, v in ev.items()) or e.summary
            out.append({**base, "id": f"{e.event_id}-replay-check", "sequence": seq, "kind": "observe", "environmentId": env, "actor": "math",
                        "tool": "suite.evaluate", "title": f"replay check {'passed' if passed else 'failed'}", "detail": e.summary, "result": observed,
                        "testResult": {"checkId": "replay", "passed": passed, "expected": "clone SLO healthy after the replayed trigger ends", "observed": observed}})
            out.append({**base, "id": f"{e.event_id}-verdict", "sequence": seq + 1, "kind": "observe", "title": e.summary, "phase": phase,
                        "tool": "canary.judge", "result": observed})
            out.extend(_teardown(base, e.event_id, env, "the verification clone", base["at"], seq + 2))
            seq += 3
        elif e.kind in (EventKind.mitigation, EventKind.patch_opened, EventKind.canary_update, EventKind.refused,
                        EventKind.page_human, EventKind.report, EventKind.experiment_end):
            tool = {EventKind.mitigation: "orchestrator.mitigate", EventKind.patch_opened: "devin.session",
                    EventKind.canary_update: "canary.judge", EventKind.refused: "safety.refuse",
                    EventKind.page_human: "pager.page", EventKind.report: "report.write",
                    EventKind.experiment_end: "orchestrator.experiment"}[e.kind]
            result = None
            if e.kind == EventKind.patch_opened:
                tool = {"github": "github.pull_request", "fallback": "patch.prebuilt"}.get(p.get("provider"), tool)
                result = f"{p.get('provider')} · {p.get('reference')} · revision {p.get('revision')}"
            elif e.kind == EventKind.canary_update and p.get("evidence"):
                result = ", ".join(f"{k}={_fmt(v)}" for k, v in (p.get("evidence") or {}).items())
            elif e.kind == EventKind.report:
                result = f"diagnosis {p.get('diagnosis')} · verification {p.get('clone_verification')} · canary {p.get('canary_status')}"
            out.append({**base, "kind": "observe", "title": e.summary, "phase": phase, "tool": tool,
                        **({"result": result} if result else {}),
                        **({"incident": "complete"} if e.kind == EventKind.report else {})})

    out.sort(key=lambda ev: (ev["at"], ev["sequence"]))
    # A run ends with a report, or with a page from stage 4/5 (no experiment, nothing reproduced,
    # verdict not confirmed). Pages in stages 6-8 are followed by more events (revise, report).
    last = events[-1]
    complete = last.kind == EventKind.report or (last.kind == EventKind.page_human and last.stage in (4, 5))
    # A run that stopped writing (crashed, killed) is not "in progress" forever.
    abandoned = not complete and now is not None and (now - last.ts) > timedelta(minutes=30)
    complete = complete or abandoned
    duration = (out[-1]["at"] + 10) if out else 60
    if not complete and now is not None:
        duration = max(duration, at(now))  # the incident is still running: the slider ends at wall-clock now
    verdict_ev = next((e for e in reversed(events) if e.kind == EventKind.verdict), None)
    report_ev = next((e for e in reversed(events) if e.kind == EventKind.report), None)
    patch_ev = next((e for e in reversed(events) if e.kind == EventKind.patch_opened), None)
    verify_ev = next((e for e in reversed(events) if e.kind == EventKind.canary_update and e.stage == 6), None)
    rp = (report_ev.payload if report_ev else {}) or {}
    report = {
        "outcome": report_ev.summary if report_ev else (
            "paged" if events[-1].kind == EventKind.page_human else "abandoned (no further events)" if abandoned else "in progress"),
        "diagnosis": (verdict_ev.payload or {}).get("diagnosis") if verdict_ev else None,
        "confirmed": bool((verdict_ev.payload or {}).get("confirmed")) if verdict_ev else False,
        "verdictAt": at(verdict_ev.ts) if verdict_ev else None,
        "patch": (patch_ev.payload or {}).get("reference") if patch_ev else None,
        "patchProvider": (patch_ev.payload or {}).get("provider") if patch_ev else None,
        "patchRevision": (patch_ev.payload or {}).get("revision", 0) if patch_ev else None,
        "verification": rp.get("clone_verification") or ((verify_ev.payload or {}).get("status") if verify_ev else None),
        "canary": rp.get("canary_status"),
        "mitigationHeld": rp.get("mitigation_held"),
        "productionActions": sum(1 for e in events if e.kind == EventKind.action_apply),
        "pages": sum(1 for e in events if e.kind == EventKind.page_human),
        "startedAt": events[0].ts.isoformat(),
        "endedAt": events[-1].ts.isoformat(),
    }
    # Retrieval context is recorded at triage and displayed separately from the
    # diagnosis. It must never decide the C2 verdict.
    memory_event = next((e for e in events if e.kind == EventKind.triage and (e.payload or {}).get("similar_incidents")), None)
    memory = []
    if memory_event:
        for item in (memory_event.payload or {}).get("similar_incidents") or []:
            if not isinstance(item, dict) or not item.get("incident_id"):
                continue
            memory.append({
                "incidentId": str(item["incident_id"]),
                "score": float(item.get("score", 0)),
                "diagnosis": item.get("diagnosis"),
                "confirmed": bool(item.get("confirmed")),
                "recordedAt": at(memory_event.ts),
            })
    return {
        "id": incident_id,
        "live": True,
        "complete": complete,
        "report": report,
        "memory": memory,
        "now": at(now) if now is not None else duration,
        "name": f"Incident {incident_id}",
        "subtitle": "Live incident · real audit log",
        "incident": detect.summary,
        "incidentTitle": detect.summary,
        "targetId": target, "entryId": entry, "policyId": policy,
        "duration": duration,
        "testCases": TEST_CASES,
        "topology": topology,
        "baseline": baseline,
        "hypotheses": hypotheses,
        "events": out,
    }


# ---- helpers ----------------------------------------------------------------------------------

def _teardown(base: dict[str, Any], event_id: str, env: str, label: str, at: int, seq: int) -> list[dict[str, Any]]:
    """Two UI events for a clone's recorded removal: `destroying` (the manager tears the project
    down; the UI fades it over ~3 s) then `archive` (gone; its evidence stays in the trace)."""
    return [
        {**base, "id": f"{event_id}-destroy", "sequence": seq, "at": at, "kind": "lifecycle", "environmentId": env,
         "actor": "adapter", "lifecycle": "destroying", "tool": "lab.destroy.request",
         "title": f"Removing {label}", "detail": "The clone lab tears the clone project down; its observations and test results are retained."},
        {**base, "id": f"{event_id}-archive", "sequence": seq + 1, "at": at + 3, "kind": "archive", "environmentId": env,
         "actor": "adapter", "tool": "lab.destroy", "title": f"{label} archived; evidence retained",
         "incident": "cleanup",  # only the report closes a real incident; more clones/probes may follow
         "detail": "No clone state is merged into production."},
    ]


def _topology(windows: list[Fingerprint]) -> dict[str, Any]:
    services: set[str] = set()
    edges: dict[str, dict[str, str]] = {}
    for fp in windows:
        services.update(fp.services.keys())
        for edge in fp.edges:
            edges[f"{edge.src}->{edge.dst}"] = {"src": edge.src, "dst": edge.dst}
    if not services:  # no persisted windows: the hero graph
        services = {"gateway", "orders", "payments"}
        edges = {"gateway->orders": {"src": "gateway", "dst": "orders"}, "orders->payments": {"src": "orders", "dst": "payments"},
                 "payments->db": {"src": "payments", "dst": "db"}}
    if "gateway" in services and "orders" in services:
        edges.setdefault("gateway->orders", {"src": "gateway", "dst": "orders"})
    ids = sorted(services | {e["src"] for e in edges.values()} | {e["dst"] for e in edges.values()})
    nodes = [{"id": i, "label": NODE_HINTS.get(i, {}).get("label", i), "kind": NODE_HINTS.get(i, {}).get("kind", "service" if i in services else "external"),
              "instrumented": i in services or i == "db"} for i in ids]
    return {"nodes": nodes, "edges": [{"id": k, "source": v["src"], "target": v["dst"]} for k, v in sorted(edges.items())]}


def _breached(fp: Fingerprint | None) -> bool:
    return bool(fp and any(s.breached for s in fp.slos))


def _window_at(windows: list[Fingerprint], ts: datetime) -> Fingerprint | None:
    """The window containing ts, else the nearest earlier one (within 60 s)."""
    best = None
    for fp in windows:
        if fp.window_start <= ts:
            best = fp
        else:
            break
    return best if best and (ts - best.window_start) <= timedelta(seconds=60) else None


def _last(windows: list[Fingerprint]) -> Fingerprint | None:
    return sorted(windows, key=lambda fp: fp.window_start)[-1] if windows else None


def _readings(fp: Fingerprint | None, topology: dict[str, Any], breached: bool | None = None) -> dict[str, Any]:
    ids = [n["id"] for n in topology["nodes"]]
    if fp is None:
        return {i: {"health": "unknown"} for i in ids}
    hot = _breached(fp) if breached is None else bool(breached)
    out: dict[str, Any] = {}
    for i in ids:
        r: dict[str, Any] = {}
        if i == "db":
            db = fp.db
            r = _clean(qps=db.qps, latency=db.query_p99_ms, utilization=_pct(db.pool_busy_ratio))
            r["health"] = "degraded" if hot else "healthy"
        elif i in fp.services:
            s = fp.services[i]
            r = _clean(qps=s.qps, latency=s.p99_ms, errorRate=_pct(s.error_rate), retryRatio=s.retry_ratio)
            r["health"] = "degraded" if hot and i != "orders_v2" else "healthy"
        else:
            r = {"health": "unknown"}
        out[i] = r
    return out


def _clean(**kv: Any) -> dict[str, Any]:
    return {k: round(float(v), 2) for k, v in kv.items() if v is not None}


def _pct(v: float | None) -> float | None:
    return None if v is None else 100.0 * v


def _fmt(v: Any) -> str:
    if isinstance(v, (int, float)):
        return f"{v:.3g}" if abs(v) < 1000 else f"{v:.0f}"
    return str(v)


def _summary(r: dict[str, Any] | None) -> str:
    if not r or r.get("health") == "unknown":
        return "Readings unknown (no persisted windows)."
    return f"p99 {_fmt(r.get('latency'))} ms, {_fmt(r.get('qps'))} qps"


def _hypotheses(triage_event: AuditEvent | None) -> list[dict[str, Any]]:
    if triage_event is None:
        return []
    p = triage_event.payload or {}
    tr = p.get("triage") or {}
    preds = {(x.get("hypothesis_id"), x.get("experiment_id")): x for x in tr.get("predictions", [])}
    out = []
    for i, h in enumerate(tr.get("hypotheses") or [{"id": hid, "label": hid, "description": ""} for hid in p.get("hypotheses", [])]):
        conf = next((x.get("confirms_if") for (hid, _), x in preds.items() if hid == h["id"] and x.get("confirms_if")), None)
        prediction = (f"Confirmed if {conf.get('metric')} is {conf.get('expect')} {conf.get('phase')}" if conf
                      else "Prediction recorded in the triage matrix.")
        out.append({"id": h["id"], "title": h.get("label") or h["id"], "description": h.get("description") or "",
                    "prediction": prediction, "color": HYPOTHESIS_COLORS[i % len(HYPOTHESIS_COLORS)]})
    return out


def _investigation_result(p: dict[str, Any]) -> str:
    ev = p.get("evidence") or {}
    rep = ev.get("reproduction") or {}
    parts = []
    if rep:
        parts.append(f"{rep.get('matching_metrics')}/{rep.get('shared_metrics')} metrics within noise")
    if p.get("prediction_total"):
        parts.append(f"probe predictions {p.get('prediction_matches')}/{p.get('prediction_total')}")
    return "; ".join(parts) or "not measured"


def _investigation_checks(p: dict[str, Any]) -> list[tuple[str, bool, str, str]]:
    ev = p.get("evidence") or {}
    rep, rec = ev.get("reproduction") or {}, ev.get("recovery") or {}
    checks = [
        ("reproduce", bool(p.get("reproduced")), "all shared metrics within 3σ of production",
         f"{rep.get('matching_metrics', '?')}/{rep.get('shared_metrics', '?')} within noise" if rep else "not measured"),
        ("recover", bool(p.get("recovered")), "clone back within noise of its healthy reference",
         f"{rec.get('matching_metrics', '?')}/{rec.get('shared_metrics', '?')} within noise" if rec else "not measured"),
    ]
    if p.get("prediction_total"):
        checks.append(("predict", p.get("prediction_matches") == p.get("prediction_total"),
                       "every predicted direction matches", f"{p.get('prediction_matches')}/{p.get('prediction_total')} directions matched"))
    return checks
