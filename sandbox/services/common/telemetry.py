import logging
import os
import re
import time


LOG_PATTERNS = {
    "orders": (
        r"payments call timed out after [0-9]+ms, retrying \(attempt [0-9]+ of [0-9]+\)",
        r"payments call failed: status [0-9]{3} \(attempt [0-9]+ of [0-9]+\)",
        r"checkout failed: payments unavailable after [0-9]+ attempts",
    ),
    "payments": (
        r"pool (primary|standby) ready \(size=[0-9]+\)",
        r"db connection pool exhausted, waited [0-9]+ms for a connection",
        r"slow database operation took [0-9]+ms",
        r"db connection error",
        r"db query failed",
    ),
    "loadgen": (
        r"generating [0-9]+\.[0-9]+ req/s",
        r"checkout request failed: status [0-9]{3}",
        r"checkout request timed out or connection unavailable",
    ),
}
_PATTERNS = {name: re.compile(r"(?:" + "|".join(patterns) + r")(?: \(\+[0-9]+ similar\))?")
             for name, patterns in LOG_PATTERNS.items()}
_providers = {}


def allowed_log(service: str, body: str) -> bool:
    pattern = _PATTERNS.get(service)
    return pattern is not None and pattern.fullmatch(body) is not None


class ApplicationLogHandler(logging.Handler):
    def __init__(self, service, otel_logger):
        super().__init__(logging.INFO)
        self.service, self.otel_logger = service, otel_logger

    def emit(self, record):
        try:
            from opentelemetry._logs import LogRecord, SeverityNumber

            body = record.getMessage()
            if record.name != self.service or not allowed_log(self.service, body):
                return
            severity = (SeverityNumber.ERROR if record.levelno >= logging.ERROR else
                        SeverityNumber.WARN if record.levelno >= logging.WARNING else SeverityNumber.INFO)
            self.otel_logger.emit(LogRecord(timestamp=int(record.created * 1_000_000_000),
                                            observed_timestamp=time.time_ns(), body=body,
                                            severity_number=severity, severity_text=severity.name,
                                            attributes={}))
        except Exception:
            return


def configure_application_logs(logger: logging.Logger) -> None:
    if (logger.name not in LOG_PATTERNS or logger.name in _providers
            or os.environ.get("FAULTLINE_OTEL_LOGS_ENABLED", "false").lower() != "true"
            or os.environ.get("OTEL_SDK_DISABLED", "false").lower() == "true"):
        return
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318").rstrip("/")
    provider = LoggerProvider(resource=Resource({"service.name": logger.name,
                                                "service.version": os.environ.get("SERVICE_VERSION", "v1")}))
    provider.add_log_record_processor(BatchLogRecordProcessor(
        OTLPLogExporter(endpoint=endpoint + "/v1/logs", headers={}, timeout=2),
        max_queue_size=512, max_export_batch_size=128, schedule_delay_millis=1000))
    logger.addHandler(ApplicationLogHandler(logger.name, provider.get_logger(logger.name)))
    _providers[logger.name] = provider
