import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from faultline_contracts import Fingerprint
from faultline_bench.comparison import (
    Event, Protocol, Run, Sample, canonical_samples, detection_time, development_cases,
    example_recording, new_recording, sample_from_fingerprint, score_run,
)
from faultline_bench.comparison_agents import (
    Diagnosis, ElasticBaseline, observable_context, openai_diagnose, safe_url, scoped_tool,
    validate_diagnosis,
)
from faultline_bench.comparison_live import WindowSampler, main, worker_command
from faultline_bench.comparison_runtime import RecordingLevers

NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
RUN_ID = "a" * 32


def sample(t, *, healthy=False, load=80, errors=None):
    return Sample(start_s=t, end_s=t + 5, metrics={
        "svc.gateway.qps": load,
        "svc.gateway.error_rate": (0.01 if healthy else 0.5) if errors is None else errors,
        "svc.gateway.p99_ms": 100 if healthy else 1500,
    }, slo_breached=not healthy)


def measured_run(arm="probe"):
    return Run(case_id="case-a", arm=arm, status="completed", samples=[
        sample(t, healthy=t < 0 or t >= 60) for t in range(-130, 120, 5)
    ], events=[Event(at_s=60, kind="detect", title="detected"), Event(at_s=90, kind="conclusion", title="diagnosed", diagnosis="H_meta")])


def test_metrics_use_fixed_horizon_and_independent_ground_truth():
    run = measured_run()
    metrics = score_run(run, development_cases()[0], Protocol(horizon_s=120))
    assert metrics.correct is True
    assert metrics.first_correct_s == 90
    assert metrics.detection_s == 60
    assert metrics.recovery_s == 60
    assert metrics.recovery_status == "recovered"
    assert metrics.failed_checkouts_estimate == pytest.approx(2448)
    assert metrics.successful_checkouts_estimate == pytest.approx(7152)
    assert metrics.sample_coverage_pct == 100
    assert metrics.tokens is None and metrics.cost_usd is None


def test_missing_window_breaks_recovery_and_does_not_impute_impact():
    run = measured_run()
    run.samples = [sample for sample in run.samples if sample.start_s != 80]
    metrics = score_run(run, development_cases()[0], Protocol(horizon_s=120))
    assert metrics.recovery_s is None
    assert metrics.recovery_status == "unknown"
    assert metrics.failed_checkouts_estimate is None
    assert metrics.sample_coverage_pct == pytest.approx(100 * 115 / 120)


def test_missing_errors_are_unknown_not_zero():
    run = measured_run()
    for window in run.samples:
        if window.start_s >= 60:
            del window.metrics["svc.gateway.error_rate"]
    metrics = score_run(run, development_cases()[0], Protocol(horizon_s=120))
    assert metrics.recovery_s is None
    assert metrics.recovery_status == "unknown"
    assert metrics.failed_checkouts_estimate is None


def test_low_load_cannot_masquerade_as_recovery():
    run = measured_run()
    for window in run.samples:
        if window.start_s >= 60:
            window.metrics["svc.gateway.qps"] = 1
            window.metrics["svc.gateway.error_rate"] = 0
    assert score_run(run, development_cases()[0], Protocol(horizon_s=120)).recovery_status == "not_recovered"


def test_passive_recovery_is_not_applicable():
    run = measured_run("observe")
    metrics = score_run(run, development_cases()[0], Protocol(horizon_s=120))
    assert metrics.correct is True
    assert metrics.recovery_status == "not_applicable"
    assert metrics.recovery_s is None


def test_revised_and_late_diagnoses_do_not_overwrite_final_with_future_answer():
    run = measured_run()
    run.events += [Event(at_s=100, kind="conclusion", title="revision", diagnosis="H_db"), Event(at_s=121, kind="conclusion", title="late", diagnosis="H_meta")]
    metrics = score_run(run, development_cases()[0], Protocol(horizon_s=120))
    assert metrics.correct is False and metrics.diagnosis == "H_db"
    assert metrics.first_correct_s == 90


def test_action_count_deduplicates_audit_and_distinguishes_clones():
    run = measured_run()
    run.events += [Event(at_s=65, kind="action_start", title="cap", environment="production", action_id="a"),
                   Event(at_s=66, kind="action_start", title="cap again in audit", environment="production", action_id="a"),
                   Event(at_s=70, kind="action_start", title="lab action", environment="clone", action_id="b"),
                   Event(at_s=125, kind="rollback_failed", title="cleanup failed")]
    metrics = score_run(run, development_cases()[0], Protocol(horizon_s=120))
    assert metrics.production_actions == 1 and metrics.clone_actions == 1
    assert metrics.rollback_failures == 1


def test_healthy_control_requires_complete_observation():
    run = Run(case_id="case-c", arm="observe", status="completed", samples=[sample(t, healthy=True) for t in range(0, 120, 5)])
    case = development_cases(True)[-1]
    assert score_run(run, case, Protocol(horizon_s=120)).correct is True
    run.samples.pop()
    assert score_run(run, case, Protocol(horizon_s=120)).correct is False


def test_synthetic_and_blocked_are_never_accuracy_evidence():
    run = measured_run()
    run.source = "synthetic"
    assert score_run(run, development_cases()[0], Protocol(horizon_s=120)).correct is None
    run.source, run.status = "live", "blocked"
    assert score_run(run, development_cases()[0], Protocol(horizon_s=120)).correct is None
    example = example_recording()
    assert all(run.metrics is None for run in example.runs)
    assert all(not run.events and not run.samples for run in example.runs if run.arm == "elastic")


def test_detection_requires_contiguous_known_breached_windows():
    protocol = Protocol(horizon_s=120)
    windows = [sample(t) for t in range(0, 60, 5)]
    assert detection_time(windows, protocol) == 60
    assert detection_time(windows[:5] + windows[6:], protocol) is None
    windows[5].slo_breached = None
    assert detection_time(windows, protocol) is None


def test_duplicate_overlapping_or_nonstandard_windows_cannot_inflate_coverage():
    windows = [sample(0), sample(0), sample(2), sample(5), Sample(start_s=10, end_s=20)]
    assert [window.start_s for window in canonical_samples(windows)] == [0, 5]
    with pytest.raises(ValueError):
        Sample(start_s=1, end_s=0)
    with pytest.raises(ValueError):
        Sample(start_s=0, end_s=5, metrics={"a": float("nan")})
    with pytest.raises(ValueError):
        Protocol(horizon_s=121)


def fingerprint():
    return Fingerprint(window_start=NOW, window_end=NOW + timedelta(seconds=5), services={}, edges=[], slos=[], db={"qps": 100})


def test_observable_context_and_no_slo_preserve_unknown():
    fp = fingerprint()
    fp.db.qps = 100
    context = observable_context([fp])[0]
    assert set(context) == {"window_start", "window_end", "metrics", "edges", "slos", "logs", "changes"}
    assert context["metrics"]["db.qps"] == 100
    assert sample_from_fingerprint(fp, NOW).slo_breached is None
    assert "expected" not in json.dumps(context) and "case-a" not in json.dumps(context)


def diagnosis_payload(diagnosis="abstain", evidence=None):
    return json.dumps({"diagnosis": diagnosis, "summary": "Insufficient evidence", "evidence": evidence or [], "hypotheses": ["H_meta", "H_db"], "recommendation": "Gather evidence"})


def test_diagnosis_allows_abstention_but_rejects_fabricated_citations():
    assert validate_diagnosis(diagnosis_payload(), [fingerprint()]).diagnosis == "abstain"
    with pytest.raises(ValueError):
        validate_diagnosis(diagnosis_payload("H_meta"), [fingerprint()])
    with pytest.raises(ValueError):
        validate_diagnosis(diagnosis_payload("H_meta", ["fake.metric"]), [fingerprint()])


def test_validate_diagnosis_tolerates_fenced_or_prefixed_json():
    payload = diagnosis_payload()
    assert validate_diagnosis("```json\n" + payload + "\n```", [fingerprint()]).diagnosis == "abstain"
    assert validate_diagnosis("Here is the assessment:\n" + payload, [fingerprint()]).diagnosis == "abstain"


def repairing_client(replies):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=replies[len(calls) - 1]))],
                               usage=SimpleNamespace(total_tokens=10))

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


def test_direct_arm_gets_one_repair_attempt_and_names_the_rejected_keys():
    client, calls = repairing_client([diagnosis_payload("H_meta", ["fake.metric"]),
                                      "```json\n" + diagnosis_payload("H_meta", ["db.qps"]) + "\n```"])
    result, tokens = openai_diagnose(client, "gpt-4.1", [fingerprint()], [])
    assert result.diagnosis == "H_meta" and result.evidence == ["db.qps"]
    assert len(calls) == 2 and tokens == 20
    assert "fake.metric" in calls[1]["messages"][-1]["content"]


def test_repair_attempt_is_bounded_and_persistent_failure_still_raises():
    client, calls = repairing_client([diagnosis_payload("H_meta", ["fake.metric"])] * 3)
    with pytest.raises(ValueError):
        openai_diagnose(client, "gpt-4.1", [fingerprint()], [])
    assert len(calls) == 2


def test_elastic_gets_the_same_repair_allowance_as_the_direct_arm():
    tool_id = scoped_tool(RUN_ID)["id"]
    replies = ["I inspected the telemetry.\n" + diagnosis_payload("H_meta", ["fake.metric"]),
               "```json\n" + diagnosis_payload("H_meta", ["db.qps"]) + "\n```"]
    converses = []

    def handler(request):
        if request.url.path.endswith("/converse"):
            converses.append(json.loads(request.content))
            return httpx.Response(200, json={"conversation_id": "conversation-1",
                                             "response": {"message": replies[len(converses) - 1]},
                                             "steps": [{"type": "tool_call", "tool_id": tool_id,
                                                        "results": [{"type": "esql", "data": []}]}]})
        return httpx.Response(200, json=scoped_tool(RUN_ID))

    client = ElasticBaseline("https://kibana.example", "https://es.example", "test-key", "test-es-key",
                             "test-inference", transport=httpx.MockTransport(handler))
    client.attach(RUN_ID)
    result, calls = client.diagnose([fingerprint()])
    assert result.diagnosis == "H_meta" and result.evidence == ["db.qps"]
    assert len(converses) == 2 and converses[1]["conversation_id"] == "conversation-1"
    assert "fake.metric" in converses[1]["input"]
    assert converses[1]["configuration_overrides"]["tools"] == [{"tool_ids": [tool_id]}]
    assert len(calls) == 2
    client.close()


def test_worker_records_why_the_responder_stopped(monkeypatch, tmp_path):
    import faultline_bench.comparison_runtime as runtime

    (tmp_path / "protocol.json").write_text(Protocol().model_dump_json())
    (tmp_path / "telemetry.json").write_text("[]")

    def explode(*args, **kwargs):
        raise ValueError("diagnosis cites unobserved metric keys")

    monkeypatch.setattr(runtime, "run_observer", explode)
    code = runtime.worker_main(["--arm", "observe", "--directory", str(tmp_path), "--origin", NOW.isoformat(),
                                "--detected-at", NOW.isoformat(), "--run-id", RUN_ID,
                                "--control-url", "http://localhost:9901", "--lab-url", "http://localhost:9910"])
    assert code == 1
    result = json.loads((tmp_path / "worker-result.json").read_text())
    assert result["error"] == "ValueError" and "unobserved metric keys" in result["detail"]
    assert "unobserved metric keys" in (tmp_path / "traceback.txt").read_text()
    last = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert "unobserved metric keys" in last["detail"]


def test_passive_receives_history_and_previous_assessment_without_forced_guess():
    calls = []
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=diagnosis_payload()))], usage=SimpleNamespace(total_tokens=42))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or response)))
    result, tokens = openai_diagnose(client, "gpt-4.1", [fingerprint()], [{"diagnosis": "abstain"}])
    assert result.diagnosis == "abstain" and tokens == 42
    request = json.loads(calls[0]["messages"][1]["content"])
    assert len(request["windows"]) == 1 and request["previous_assessments"]
    assert "You may abstain" in calls[0]["messages"][0]["content"]


def test_scoped_esql_is_locked_to_one_opaque_run_and_no_outcomes():
    tool = scoped_tool(RUN_ID)
    assert tool["configuration"]["params"] == {}
    query = tool["configuration"]["query"]
    assert f'run_id == "{RUN_ID}"' in query
    assert "LIMIT 240" in query and "faultline-audit" not in query
    with pytest.raises(ValueError):
        scoped_tool('" | FROM *')


@pytest.mark.parametrize("url", ["http://remote.example", "https://user:secret@example.com", "https://example.com?key=x", "https://example.com:99999", "https://example.com/\n"])
def test_elastic_urls_reject_unsafe_origins(url):
    with pytest.raises(ValueError):
        safe_url(url)


def test_elastic_uses_native_tool_and_explicit_model_without_fallback():
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.startswith("/_inference/"):
            return httpx.Response(200, json={"endpoints": [{"service": "openai", "service_settings": {"model_id": "gpt-4.1"}}]})
        if request.method in ("HEAD", "GET"):
            return httpx.Response(404)
        if request.url.path.endswith("/converse"):
            body = json.loads(request.content)
            assert body["inference_id"] == "test-inference"
            assert body["configuration_overrides"]["tools"] == [{"tool_ids": [scoped_tool(RUN_ID)["id"]]}]
            assert body["configuration_overrides"]["enable_elastic_capabilities"] is False
            return httpx.Response(200, json={"conversation_id": "conversation-1", "response": {"message": diagnosis_payload()}, "steps": [{"type": "tool_call", "tool_id": scoped_tool(RUN_ID)["id"], "results": [{"type": "esql", "data": []}]}]})
        return httpx.Response(200, json={"errors": False})
    client = ElasticBaseline("https://kibana.example", "https://es.example", "test-key", "test-es-key", "test-inference", transport=httpx.MockTransport(handler))
    client.prepare(RUN_ID, "gpt-4.1")
    client.publish(RUN_ID, [fingerprint()])
    first_count = len(requests)
    client.publish(RUN_ID, [fingerprint()])
    assert len(requests) == first_count
    result, calls = client.diagnose([fingerprint()])
    assert result.diagnosis == "abstain" and calls
    assert client.conversation_id == "conversation-1"
    client.close()


def test_elastic_errors_never_include_remote_body_or_credentials():
    client = ElasticBaseline("https://kibana.example", "https://es.example", "test-key", "test-es-key", "test-inference", transport=httpx.MockTransport(lambda _: httpx.Response(401, text="secret response")))
    with pytest.raises(RuntimeError, match="HTTP 401") as error:
        client.prepare(RUN_ID, "gpt-4.1")
    assert "secret" not in str(error.value) and "test-key" not in str(error.value)
    client.close()


def test_no_actions_or_network_without_explicit_execution(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(httpx.Client, "request", lambda *a, **kw: pytest.fail("unexpected network"))
    assert main(["--output", str(tmp_path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert len(plan["cases"]) == 2 and len(plan["runs"]) == 8
    assert list(tmp_path.iterdir()) == []
    assert main(["--cases", "case-a", "--arms", "observe", "probe"]) == 0
    small_plan = json.loads(capsys.readouterr().out)
    assert len(small_plan["cases"]) == 1 and len(small_plan["runs"]) == 2
    with pytest.raises(SystemExit):
        main(["--execute"])
    with pytest.raises(SystemExit):
        main(["--execute", "--exclusive-sandbox"])
    assert main(["--demo", "--output", str(tmp_path)]) == 0
    assert (tmp_path / "cmp-example.json").exists()


def test_worker_arguments_do_not_contain_fault_labels_or_case_parameters(tmp_path):
    run = new_recording(["probe"], Protocol()).runs[0]
    args = SimpleNamespace(control_url="http://localhost:9901", lab_url="http://localhost:9910")
    command = worker_command(run, tmp_path / run.id, NOW, NOW, args)
    rendered = " ".join(command)
    assert run.id in rendered
    assert "storm" not in rendered and "capacity_qps" not in rendered and "expected" not in rendered


def test_sampler_rejects_stale_snapshots(monkeypatch):
    sampler = WindowSampler(None)
    sampler.snapshots.extend([(NOW - timedelta(seconds=10), {}), (NOW + timedelta(seconds=5), {})])
    monkeypatch.setattr("faultline_bench.comparison_live.fingerprint_from_snapshots", lambda *a: pytest.fail("stale input reached canonical builder"))
    assert sampler.window(NOW, NOW + timedelta(seconds=5)) is None


def test_recording_levers_refuse_canary_budget_and_late_actions():
    inner = SimpleNamespace(apply=lambda *args: pytest.fail("forbidden action"))
    adapter = RecordingLevers(inner, None, datetime(2000, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(RuntimeError):
        adapter.apply("canary_weight", {}, 5)
    with pytest.raises(RuntimeError):
        adapter.apply("retry_cap", {}, 5)


def test_healthy_control_does_not_overwrite_an_incorrect_assertion():
    run = Run(case_id="case-c", arm="observe", status="completed", samples=[sample(t, healthy=True) for t in range(0, 120, 5)],
              events=[Event(at_s=90, kind="conclusion", title="wrong", diagnosis="H_db")])
    result = score_run(run, development_cases(True)[-1], Protocol(horizon_s=120))
    assert result.correct is False and result.diagnosis == "H_db"


def test_recording_id_cannot_escape_output_directory():
    recording = example_recording().model_dump()
    recording["id"] = "../../outside"
    from faultline_bench.comparison import Recording
    with pytest.raises(ValueError):
        Recording.model_validate(recording)


def test_elastic_attach_rejects_broadened_tools():
    tool = scoped_tool(RUN_ID)
    tool["configuration"]["query"] = "FROM * | LIMIT 240"
    client = ElasticBaseline("https://kibana.example", "https://es.example", "test-key", "test-es-key", "test-inference", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=tool)))
    with pytest.raises(RuntimeError, match="frozen definition"):
        client.attach(RUN_ID)
    client.close()


def test_cleanup_destroys_only_this_runs_clones(monkeypatch, tmp_path):
    from faultline_contracts.clone import CloneStatus
    from faultline_bench.comparison_live import cleanup_owned
    run = Run(id=RUN_ID, case_id="case-a", arm="clone_probe")
    owned = SimpleNamespace(clone_id="owned", spec=SimpleNamespace(name="cmp-" + RUN_ID[:16] + "-abc"), status=CloneStatus.ready)
    unrelated = SimpleNamespace(clone_id="other", spec=SimpleNamespace(name="someone-elses-clone"), status=CloneStatus.ready)
    destroyed = []
    lab = SimpleNamespace(list=lambda: [owned, unrelated], destroy=lambda cid: destroyed.append(cid) or SimpleNamespace(status=CloneStatus.destroyed))
    monkeypatch.setattr("faultline_bench.comparison_live.HttpCloneLab", lambda *a, **kw: lab)
    log = SimpleNamespace(emit=lambda *a, **kw: None)
    assert cleanup_owned(run, tmp_path, SimpleNamespace(lab_url="http://localhost:9910"), log)
    assert destroyed == ["owned"]


def test_probe_pipeline_keeps_detection_baseline_and_stops_before_shipping(monkeypatch, tmp_path):
    import openai
    import faultline_bench.comparison_runtime as runtime
    calls = []
    client = SimpleNamespace(close=lambda: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: client)
    class Pipeline:
        def __init__(self, *args, **kwargs):
            self.judgments = 0
        def detect(self, *args):
            return fingerprint()
        def triage(self, *args):
            return "triage"
        def plan(self, *args):
            return "probe"
        def experiment(self, iid, experiment, now):
            calls.append(("experiment", experiment, now))
            return [], [], []
        def judge(self, *args):
            self.judgments += 1
            return SimpleNamespace(confirmed=self.judgments == 2)
        def confirmation_experiment(self, *args):
            return "confirmation"
        def mitigate(self, *args):
            calls.append(("mitigate",))
    monkeypatch.setattr(runtime, "Orchestrator", Pipeline)
    log = SimpleNamespace(emit=lambda *a, **kw: None)
    runtime.run_probe("probe", None, tmp_path, RUN_ID, Protocol(), log, NOW + timedelta(days=365), "http://localhost:9901", "http://localhost:9910", NOW)
    assert calls == [("experiment", "probe", NOW), ("experiment", "confirmation", NOW), ("mitigate",)]


@pytest.mark.parametrize("ignite", [True, False])
def test_live_loop_records_all_windows_and_retains_nonignition(monkeypatch, tmp_path, ignite):
    import faultline_bench.comparison_live as live
    import faultline_bench.comparison_runtime as runtime
    now = [NOW]
    monkeypatch.setattr(live, "utcnow", lambda: now[0])
    monkeypatch.setattr(runtime, "utcnow", lambda: now[0])
    monkeypatch.setattr(live.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + timedelta(seconds=seconds)))
    class Sampler:
        def __init__(self, source):
            pass
        def start(self):
            pass
        def stop(self):
            pass
        def window(self, start, end):
            breached = ignite and start >= NOW + timedelta(seconds=132)
            return Fingerprint(window_start=start, window_end=end,
                services={"gateway": {"qps": 80, "error_rate": 0.5 if breached else 0.0, "p99_ms": 1500 if breached else 100}},
                slos=[{"name": "checkout", "metric": "svc.gateway.p99_ms", "threshold": 1000, "value": 1500 if breached else 100, "breached": breached}])
    monkeypatch.setattr(live, "WindowSampler", Sampler)
    monkeypatch.setattr(live, "cleanup_owned", lambda *a: True)
    launched = []
    class Process:
        def __init__(self, command, **kwargs):
            launched.append(command)
            directory = tmp_path / ".work" / recording.runs[0].id
            (directory / "worker-result.json").write_text(json.dumps({"status": "completed"}))
            runtime.EventLog(directory, NOW + timedelta(seconds=132)).emit("conclusion", "test answer", diagnosis="H_meta")
        def poll(self):
            return 0
    monkeypatch.setattr(live.subprocess, "Popen", Process)
    injections, resets = [], []
    controller = SimpleNamespace(storm=lambda fault: injections.append(fault), reset=lambda: resets.append(True))
    args = SimpleNamespace(output=tmp_path, orders_url="http://localhost:8101", payments_url="http://localhost:8102", loadgen_url="http://localhost:8103", control_url="http://localhost:9901", lab_url="http://localhost:9910")
    recording = new_recording(["probe"], Protocol(horizon_s=120))
    run = recording.runs[0]
    assert live.run_case(recording, run, recording.cases[0], args, controller)
    assert len(injections) == 1 and len(resets) == 1
    assert run.status == "completed"
    assert run.metrics.sample_coverage_pct == 100
    assert run.metrics.correct is ignite
    assert len(launched) == int(ignite)
    assert (tmp_path / (recording.id + ".json")).exists()
