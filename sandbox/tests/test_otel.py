import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, Mock, patch
from urllib.request import Request, urlopen

from services.common.telemetry import ApplicationLogHandler, allowed_log, configure_application_logs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from validate_lab import Run

IMAGE = "otel/opentelemetry-collector-contrib:0.160.0"


def command(*args, env=None):
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError(f"{args[0]} failed: {result.stderr}")
    return result.stdout


def compose_env(a="", b="", tee=""):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("FAULTLINE_", "OTEL_", "COMPOSE_"))}
    env.update(FAULTLINE_ELASTICSEARCH_URL=a, FAULTLINE_ELASTICSEARCH_API_KEY="test-a",
               FAULTLINE_OTLP_ENDPOINT=b, FAULTLINE_OTLP_API_KEY="test-b", FAULTLINE_OTEL_TEE=tee)
    return env


class ApplicationLogTests(unittest.TestCase):
    def test_allowlist_and_sensitive_messages(self):
        for service, body in (("orders", "checkout failed: payments unavailable after 4 attempts"),
                              ("payments", "db connection pool exhausted, waited 1000ms for a connection (+3 similar)"),
                              ("loadgen", "generating 80.0 req/s")):
            self.assertTrue(allowed_log(service, body))
            self.assertFalse(allowed_log(service, body + " io_profile=800"))
            self.assertFalse(allowed_log("faultctl", body))
        for body in ("db query failed: SELECT io_profile", "world=storm", "/admin/retry_override",
                     "db target set to standby for 60s", "rate set to 0.0 req/s"):
            self.assertFalse(allowed_log("payments", body))

    def test_handler_does_not_export_exceptions_or_extra_attributes(self):
        sink = Mock()
        handler = ApplicationLogHandler("payments", sink)
        record = logging.LogRecord("payments", logging.ERROR, "private.py", 1,
                                   "db query failed", (), (ValueError, ValueError("hidden"), None))
        record.api_key = "secret"
        handler.emit(record)
        exported = sink.emit.call_args.args[0]
        self.assertEqual(exported.body, "db query failed")
        self.assertEqual(exported.attributes, {})
        handler.emit(logging.LogRecord("faultctl", logging.ERROR, "", 1, "db query failed", (), None))
        self.assertEqual(sink.emit.call_count, 1)

    def test_disabled_and_control_loggers_never_create_provider(self):
        with patch.dict(os.environ, {"FAULTLINE_OTEL_LOGS_ENABLED": "true", "OTEL_SDK_DISABLED": "true"}):
            logger = Mock(name="orders")
            logger.name = "orders"
            configure_application_logs(logger)
            logger.addHandler.assert_not_called()
        with patch.dict(os.environ, {"FAULTLINE_OTEL_LOGS_ENABLED": "true", "OTEL_SDK_DISABLED": "false"}):
            for name in ("faultctl", "control", "lab"):
                logger = Mock()
                logger.name = name
                configure_application_logs(logger)
                logger.addHandler.assert_not_called()

    def test_fairness_reader_includes_log_body_and_nested_values(self):
        rec = {"resourceLogs": [{"resource": {"attributes": [{"key": "world", "value": {"stringValue": "storm"}}]},
                                "scopeLogs": [{"logRecords": [{"body": {"stringValue": "io_profile"},
                                    "attributes": [{"key": "nested", "value": {"kvlistValue": {"values": [
                                        {"key": "hidden", "value": {"stringValue": "trigger"}}]}}}]}]}]}]}
        self.assertTrue({"world", "storm", "io_profile", "trigger"} <= Run._record_strings(rec))

    def test_b_indexing_check_never_falls_back_to_a(self):
        with patch.dict(os.environ, {"FAULTLINE_OTLP_ENDPOINT": "https://example.invalid",
                                     "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_URL": "",
                                     "FAULTLINE_OBSERVABILITY_ELASTICSEARCH_API_KEY": ""}), \
                patch.object(Run, "_root_env", return_value=[]), patch("validate_lab.httpx.post") as post:
            Run.__new__(Run)._elastic_check("clone-1")
            post.assert_not_called()

    def test_clone_environment_and_stale_tee_cleanup(self):
        from faultline_contracts.clone import CloneSpec
        from services.lab import app as lab
        with tempfile.TemporaryDirectory() as tmp, patch.object(lab, "SANDBOX_DIR", Path(tmp)):
            clone = lab.Clone("test-clone", 2, CloneSpec(name="test"))
            env = clone.env()
            self.assertEqual(env["OTEL_DEPLOYMENT_ENVIRONMENT"], "clone-2")
            self.assertEqual(env["FAULTLINE_OTEL_TEE"], "1")
            self.assertEqual(env["ORDERS_V2_IMAGE"], "faultline-clone-orders-v2:2")
            stale = Path(env["FAULTLINE_OTEL_TEE_DIR"]) / "records.jsonl"
            stale.write_text("old clone evidence")
            other = stale.with_name("keep.txt")
            other.write_text("keep")
            clone.compose = AsyncMock()
            clone.init_db_cost = AsyncMock()
            asyncio.run(clone.start())
            self.assertFalse(stale.exists())
            self.assertTrue(other.exists())


@unittest.skipUnless(os.environ.get("SANDBOX_OTEL_DOCKER_TESTS") == "1", "standalone Docker tests opt-in")
class CollectorTests(unittest.TestCase):
    def test_sink_matrix_and_pinned_validation(self):
        for a in ("", "https://a.invalid"):
            for b in ("", "https://b.invalid"):
                for tee in ("", "1"):
                    with self.subTest(a=bool(a), b=bool(b), tee=bool(tee)):
                        config = json.loads(command("docker", "compose", "--env-file", "/dev/null", "--profile", "canary",
                                                    "config", "--format", "json", env=compose_env(a, b, tee)))
                        collector = config["services"]["otel-collector"]
                        self.assertEqual(collector["image"], IMAGE)
                        sink = "sink" + ("-elastic" if a else "") + ("-otlp" if b else "") + ("-tee" if tee else "") + ".yaml"
                        self.assertEqual(collector["command"][-1], "--config=/etc/otelcol/" + sink)
                        for name in ("orders", "orders-v2", "payments", "loadgen"):
                            svc = config["services"][name]
                            self.assertEqual(svc["command"][0], "opentelemetry-instrument")
                            self.assertEqual(svc["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://otel-collector:4318")
                            self.assertEqual(svc["environment"]["FAULTLINE_OTEL_LOGS_ENABLED"], "true")
                            self.assertEqual(svc["environment"]["OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED"], "false")
                        self.assertEqual(config["services"]["faultctl"]["environment"]["OTEL_SDK_DISABLED"], "true")
                        command("docker", "run", "--rm", "-v", f"{ROOT / 'otel'}:/etc/otelcol:ro",
                                "-e", "OTEL_DEPLOYMENT_ENVIRONMENT=clone-2",
                                "-e", "FAULTLINE_OTLP_ENDPOINT=https://b.invalid", "-e", "FAULTLINE_OTLP_API_KEY=test-b",
                                "-e", "FAULTLINE_ELASTICSEARCH_URL=https://a.invalid", "-e", "FAULTLINE_ELASTICSEARCH_API_KEY=test-a",
                                IMAGE, "validate", *collector["command"])
                        text = (ROOT / "otel" / sink).read_text()
                        if b:
                            self.assertNotIn("elasticsearch/elastic", text)
                            self.assertIn("otlphttp/observability", text)
                        for signal in ("traces", "metrics", "logs"):
                            if b or a or tee:
                                self.assertIn(signal + ":", text)

    def test_clone_networks_and_empty_or_missing_sink_variables(self):
        from services.lab.app import CLONE_SERVICES
        self.assertNotIn("faultctl", CLONE_SERVICES)
        env = compose_env()
        for key in ("FAULTLINE_OTLP_ENDPOINT", "FAULTLINE_ELASTICSEARCH_URL", "FAULTLINE_OTEL_TEE"):
            env.pop(key)
        config = json.loads(command("docker", "compose", "--env-file", "/dev/null", "config", "--format", "json", env=env))
        self.assertTrue(config["services"]["otel-collector"]["command"][-1].endswith("/sink.yaml"))
        env.update(FAULTLINE_OTLP_ENDPOINT="https://b.invalid", FAULTLINE_OTLP_API_KEY="",
                   FAULTLINE_ELASTICSEARCH_URL="https://a.invalid", FAULTLINE_OTEL_TEE="1")
        config = json.loads(command("docker", "compose", "--env-file", "/dev/null", "-f", "docker-compose.yml",
                                    "-f", "clone.override.yml", "--profile", "canary", "config", "--format", "json", env=env))
        collector = config["services"]["otel-collector"]
        self.assertTrue(collector["command"][-1].endswith("/sink-elastic-otlp-tee.yaml"))
        self.assertEqual(set(collector["networks"]), {"default", "egress"})
        for name in ("orders", "orders-v2", "payments", "loadgen", "control"):
            self.assertEqual(set(config["services"][name]["networks"]), {"default"})
        self.assertEqual(config["networks"]["default"]["driver_opts"]["com.docker.network.bridge.enable_ip_masquerade"], "false")

    def test_real_application_logs_and_collector_filtering_to_b_with_tee(self):
        received = []

        class Sink(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers.get("Authorization"),
                                 self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("0.0.0.0", 0), Sink)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        name = "sandbox-otel-test-" + uuid.uuid4().hex[:10]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                Path(tmp).chmod(0o777)
                command("docker", "run", "--rm", "-d", "--name", name,
                        "-p", "127.0.0.1::4318", "-v", f"{ROOT / 'otel'}:/etc/otelcol:ro", "-v", f"{tmp}:/tmp/otel",
                        "-e", "OTEL_DEPLOYMENT_ENVIRONMENT=clone-2",
                        "-e", f"FAULTLINE_OTLP_ENDPOINT=http://host.docker.internal:{server.server_port}",
                        "-e", "FAULTLINE_OTLP_API_KEY=test-b", "-e", "FAULTLINE_ELASTICSEARCH_URL=http://unused.invalid",
                        IMAGE, "--config=/etc/otelcol/collector.yaml", "--config=/etc/otelcol/sink-elastic-otlp-tee.yaml")
                port = command("docker", "port", name, "4318/tcp").strip().rsplit(":", 1)[1]
                endpoint = f"http://127.0.0.1:{port}"

                def post(signal, payload):
                    request = Request(endpoint + "/v1/" + signal, json.dumps(payload).encode(),
                                      {"Content-Type": "application/json"})
                    for attempt in range(30):
                        try:
                            with urlopen(request, timeout=2) as response:
                                self.assertEqual(response.status, 200)
                            return
                        except OSError:
                            if attempt == 29:
                                raise
                            time.sleep(0.2)

                attrs = [{"key": "service.name", "value": {"stringValue": "payments"}},
                         {"key": "world", "value": {"stringValue": "secret-world"}}]
                records = [{"body": {"stringValue": body}, "attributes": [
                    {"key": "db.statement", "value": {"stringValue": "SELECT io_profile"}},
                    {"key": "api_key", "value": {"stringValue": "private-key"}}]} for body in
                           ("db query failed", "world=storm", "/internal/db_target", "SELECT io_profile", "db query failed: private-key")]
                post("logs", {"resourceLogs": [
                    {"resource": {"attributes": attrs}, "scopeLogs": [{"scope": {"name": "private-scope"}, "logRecords": records}]},
                    {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "faultctl"}}]},
                     "scopeLogs": [{"logRecords": [{"body": {"stringValue": "db query failed"}}]}]}]})
                app_env = compose_env()
                app_env.update(OTEL_EXPORTER_OTLP_ENDPOINT=endpoint, OTEL_SDK_DISABLED="false", FAULTLINE_OTEL_LOGS_ENABLED="true",
                               OTEL_TRACES_EXPORTER="none", OTEL_METRICS_EXPORTER="none", OTEL_LOGS_EXPORTER="none",
                               OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED="false")
                command("opentelemetry-instrument", sys.executable, "-c", '''
import asyncio
from unittest.mock import AsyncMock, Mock
from services.orders import app as orders
from services.payments import app as payments
from services.loadgen import app as loadgen
from services.common.telemetry import _providers
async def main():
    orders._attempt = AsyncMock(return_value=(False, "status 503"))
    await orders.checkout()
    payments.pools["primary"] = Mock(acquire=AsyncMock(side_effect=asyncio.TimeoutError))
    await payments._run_query("test", 1)
    loadgen._session = Mock()
    loadgen._session.post.return_value.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError)
    loadgen._session.post.return_value.__aexit__ = AsyncMock()
    await loadgen._fire()
asyncio.run(main())
for provider in _providers.values():
    assert provider.force_flush()
    provider.shutdown()
''', env=app_env)
                now = time.time_ns()
                clean_attrs = attrs[:1]
                post("traces", {"resourceSpans": [{"resource": {"attributes": clean_attrs}, "scopeSpans": [{"spans": [
                    {"traceId": "1234567890abcdef1234567890abcdef", "spanId": "1234567890abcdef", "name": "checkout",
                     "startTimeUnixNano": str(now), "endTimeUnixNano": str(now + 1000000)},
                    {"traceId": "1234567890abcdef1234567890abcdef", "spanId": "2234567890abcdef", "name": "internal-hidden",
                     "attributes": [{"key": "url.path", "value": {"stringValue": "/internal/hidden"}}],
                     "startTimeUnixNano": str(now), "endTimeUnixNano": str(now + 1000000)}]}]}]})
                post("metrics", {"resourceMetrics": [{"resource": {"attributes": clean_attrs}, "scopeMetrics": [{"metrics": [
                    {"name": metric, "gauge": {"dataPoints": [{"timeUnixNano": str(now), "asInt": "1"}]}}
                    for metric in ("requests", "envoy.fault.hidden")]}]}]})
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if {path for path, _, _ in received} >= {"/v1/logs", "/v1/traces", "/v1/metrics"}:
                        break
                    time.sleep(0.2)
                command("docker", "stop", name)
                output = (Path(tmp) / "records.jsonl").read_text()
                decoded = [json.loads(line) for line in output.splitlines()]
                strings = set().union(*(Run._record_strings(record) for record in decoded))
                for forbidden in ("secret-world", "private-key", "private-scope", "io_profile", "internal-hidden", "envoy.fault.hidden", "faultctl"):
                    self.assertNotIn(forbidden, output)
                self.assertIn("db query failed", strings)
                self.assertIn("checkout failed: payments unavailable after 4 attempts", strings)
                self.assertIn("checkout request timed out or connection unavailable", strings)
                self.assertTrue(any(value.startswith("db connection pool exhausted") for value in strings))
                self.assertIn("deployment.environment", strings)
                self.assertIn("deployment.environment.name", strings)
                self.assertIn("clone-2", strings)
                self.assertEqual({path for path, _, _ in received}, {"/v1/logs", "/v1/traces", "/v1/metrics"})
                self.assertTrue(all(auth == "ApiKey test-b" for _, auth, _ in received))
                from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
                import gzip
                remote_logs = []
                for path, _, body in received:
                    if path == "/v1/logs":
                        if body.startswith(b"\x1f\x8b"):
                            body = gzip.decompress(body)
                        request = ExportLogsServiceRequest.FromString(body)
                        remote_logs.extend(log.body.string_value for resource in request.resource_logs
                                           for scope in resource.scope_logs for log in scope.log_records)
                self.assertIn("checkout failed: payments unavailable after 4 attempts", remote_logs)
                self.assertNotIn("world=storm", remote_logs)
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
