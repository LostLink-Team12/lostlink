"""JSON logging and low-cardinality HTTP metrics without request-body logging."""

import json
import logging
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import UUID, uuid4

from prometheus_client import CollectorRegistry, Counter, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_context.get()),
        }
        for key in ("method", "route", "status", "duration_ms", "error_type"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        # Exception messages can contain SQL parameters, credentials, or personal data.
        if record.exc_info and record.exc_info[0]:
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload)


def configure_logging(service_name: str, level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service_name))
    logger = logging.getLogger("lostlink")
    logger.handlers = [handler]
    logger.setLevel(level)
    logger.propagate = False


class HttpMetrics:
    def __init__(self, service_name: str):
        self.registry = CollectorRegistry()
        self.service_name = service_name
        labels = ("service", "method", "route", "status")
        self.requests = Counter(
            "lostlink_http_requests_total",
            "Completed HTTP requests",
            labels,
            registry=self.registry,
        )
        self.duration = Histogram(
            "lostlink_http_request_duration_seconds",
            "HTTP request duration",
            labels,
            registry=self.registry,
        )


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp, metrics: HttpMetrics):
        self.app = app
        self.metrics = metrics
        self.logger = logging.getLogger("lostlink.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            request_id = str(UUID(headers.get(b"x-request-id", b"").decode("ascii")))
        except (ValueError, UnicodeError):
            request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        context_token = request_id_context.set(request_id)
        started = time.perf_counter()
        status = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ] + [(b"x-request-id", request_id.encode("ascii"))]
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            route = getattr(scope.get("route"), "path", "__unmatched__")
            method = scope["method"]
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                method = "OTHER"
            duration = time.perf_counter() - started
            if route not in {"/health", "/ready", "/metrics"}:
                labels = (self.metrics.service_name, method, route, str(status))
                self.metrics.requests.labels(*labels).inc()
                self.metrics.duration.labels(*labels).observe(duration)
                self.logger.info(
                    "http_request",
                    extra={
                        "request_id": request_id,
                        "method": method,
                        "route": route,
                        "status": status,
                        "duration_ms": round(duration * 1000, 2),
                    },
                )
            request_id_context.reset(context_token)
