"""LostLink service factory with explicit dependency lifecycle and safe defaults."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from lostlink_common.auth import CognitoVerifier, TokenVerifier
from lostlink_common.config import Settings
from lostlink_common.db import Database
from lostlink_common.errors import install_error_handlers
from lostlink_common.events import SnsPublisher, SqsPublisher, aws_client
from lostlink_common.observability import HttpMetrics, ObservabilityMiddleware, configure_logging
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.models import DATABASE_REVISION, DATABASE_SCHEMA
from app.routes import router

logger = logging.getLogger("lostlink.readiness")


def create_app(
    settings: Settings | None = None,
    *,
    token_verifier: TokenVerifier | None = None,
    database: Database | None = None,
) -> FastAPI:
    settings = settings or Settings()
    metrics = HttpMetrics(settings.service_name)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configure_logging(settings.service_name, settings.log_level)
        owned_database = database is None and settings.database_url is not None
        if owned_database:
            application.state.database = Database(settings)
        clients = []
        try:
            if settings.event_topic_arn:
                client = aws_client("sns", settings)
                clients.append(client)
                application.state.event_publisher = SnsPublisher(client, settings.event_topic_arn)
            if settings.event_queue_url:
                client = aws_client("sqs", settings)
                clients.append(client)
                application.state.queue_publisher = SqsPublisher(client, settings.event_queue_url)
            yield
        finally:
            for client in clients:
                client.close()
            if owned_database:
                application.state.database.close()

    application = FastAPI(title=settings.service_name, version="0.1.0", lifespan=lifespan)
    application.state.settings = settings
    application.state.database = database
    application.state.token_verifier = token_verifier or (
        CognitoVerifier(settings) if settings.cognito_issuer else None
    )
    application.state.event_publisher = None
    application.state.queue_publisher = None
    application.state.metrics = metrics
    application.add_middleware(ObservabilityMiddleware, metrics=metrics)
    install_error_handlers(application)
    application.include_router(router)

    @application.get("/health", tags=["Operations"])
    def health() -> dict:
        return {"status": "ok", "service": settings.service_name}

    @application.get("/ready", tags=["Operations"])
    def ready() -> JSONResponse:
        checks = {
            "identity": "configured" if application.state.token_verifier else "not_configured",
            "database": "not_configured",
        }
        db = application.state.database
        if db is not None:
            try:
                db.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
                checks["database"] = "ready"
            except Exception as exc:
                checks["database"] = "unavailable"
                logger.warning("readiness_failed", extra={"error_type": type(exc).__name__})
        is_ready = checks["identity"] == "configured" and checks["database"] == "ready"
        return JSONResponse(
            {"status": "ready" if is_ready else "not_ready", "checks": checks},
            status_code=200 if is_ready else 503,
        )

    @application.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        return Response(
            generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    return application


app = create_app()
