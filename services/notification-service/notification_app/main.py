"""Notification HTTP lifecycle; consumers and senders run as separate worker processes."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from lostlink_common.auth import CognitoVerifier
from lostlink_common.db import Database
from lostlink_common.errors import install_error_handlers
from lostlink_common.observability import HttpMetrics, ObservabilityMiddleware, configure_logging
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from notification_app.config import NotificationSettings
from notification_app.errors import install_notification_errors
from notification_app.models import DATABASE_REVISION, DATABASE_SCHEMA
from notification_app.routes import router

logger = logging.getLogger("lostlink.notification")


def create_app(settings=None, *, token_verifier=None, database=None, admin_database=None):
    settings = settings or NotificationSettings()
    metrics = HttpMetrics(settings.service_name)

    @asynccontextmanager
    async def lifespan(application):
        configure_logging(settings.service_name, settings.log_level)
        owned = []
        try:
            for name, supplied in [("database", database), ("admin_database", admin_database)]:
                url = getattr(settings, f"{name}_url")
                if supplied is None and url:
                    db = Database(settings.model_copy(update={"database_url": url}))
                    setattr(application.state, name, db)
                    owned.append(db)
            yield
        finally:
            for db in owned:
                db.close()

    app = FastAPI(title="LostLink Notification Service", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.database, app.state.admin_database = database, admin_database
    app.state.token_verifier = token_verifier or (
        CognitoVerifier(settings) if settings.cognito_issuer else None
    )
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
    install_error_handlers(app)
    install_notification_errors(app)
    app.include_router(router)

    @app.get("/health", tags=["Operations"])
    def health():
        return {"status": "ok", "service": settings.service_name}

    @app.get("/ready", tags=["Operations"])
    def ready():
        checks = {
            "identity": bool(app.state.token_verifier),
            "clients": bool(settings.user_client_ids),
        }
        try:
            if app.state.database is None:
                raise RuntimeError("Database is not configured")
            app.state.database.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
            checks["database"] = True
        except Exception as exc:
            checks["database"] = False
            logger.warning(
                "notification_readiness_failed", extra={"error_type": type(exc).__name__}
            )
        # The operator API is optional; when configured it must also be current.
        if app.state.admin_database is not None:
            try:
                app.state.admin_database.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
                checks["admin_database"] = True
            except Exception:
                checks["admin_database"] = False
        ready = all(checks.values())
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint():
        return Response(
            generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    return app


app = create_app()
