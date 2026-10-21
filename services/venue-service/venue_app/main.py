"""Venue HTTP process; workers and migrations are separate entrypoints."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from lostlink_common.auth import CognitoVerifier
from lostlink_common.errors import install_error_handlers
from lostlink_common.observability import HttpMetrics, ObservabilityMiddleware, configure_logging
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from venue_app.config import VenueSettings
from venue_app.database import VenueDatabase
from venue_app.errors import install_venue_errors
from venue_app.models import DATABASE_REVISION, DATABASE_SCHEMA
from venue_app.routes import router

logger = logging.getLogger("lostlink.venue")


def create_app(
    settings=None,
    *,
    token_verifier=None,
    database=None,
    platform_database=None,
    identity_database=None,
):
    settings = settings or VenueSettings()
    metrics = HttpMetrics(settings.service_name)

    @asynccontextmanager
    async def lifespan(application):
        configure_logging(settings.service_name, settings.log_level)
        owned = []
        try:
            for name, supplied in [
                ("database", database),
                ("platform_database", platform_database),
                ("identity_database", identity_database),
            ]:
                url = getattr(settings, f"{name}_url")
                if supplied is None and url:
                    db = VenueDatabase(settings.model_copy(update={"database_url": url}))
                    setattr(application.state, name, db)
                    owned.append(db)
            yield
        finally:
            for db in owned:
                db.close()

    app = FastAPI(title="LostLink Venue Service", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.database, app.state.platform_database = database, platform_database
    app.state.identity_database = identity_database
    app.state.token_verifier = token_verifier or (
        CognitoVerifier(settings) if settings.cognito_issuer else None
    )
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
    install_error_handlers(app)
    install_venue_errors(app)
    app.include_router(router)

    @app.get("/health", tags=["Operations"])
    def health():
        return {"status": "ok", "service": settings.service_name}

    @app.get("/ready", tags=["Operations"])
    def ready():
        checks = {
            "identity": bool(app.state.token_verifier),
            "clients": bool(settings.user_client_ids and settings.platform_client_ids),
        }
        names = ["database", "platform_database"]
        if settings.identity_resolve_client_ids:
            names.append("identity_database")
        for name in names:
            db = getattr(app.state, name)
            try:
                if db is None:
                    raise RuntimeError("Database is not configured")
                db.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
                checks[name] = True
            except Exception as exc:
                checks[name] = False
                logger.warning("venue_readiness_failed", extra={"error_type": type(exc).__name__})
        ready = all(checks.values())
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics():
        return Response(
            generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    return app


app = create_app()
