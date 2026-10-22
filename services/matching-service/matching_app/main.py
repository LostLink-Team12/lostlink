"""Stateless Matching API. Durable jobs run in separate worker processes."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from lostlink_common.auth import CognitoVerifier
from lostlink_common.errors import install_error_handlers
from lostlink_common.observability import HttpMetrics, ObservabilityMiddleware, configure_logging
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from matching_app.clients import make_item_client
from matching_app.config import MatchingSettings
from matching_app.database import MatchingDatabase
from matching_app.embedding import ClipEncoder
from matching_app.errors import install_matching_errors
from matching_app.limits import BodyLimitMiddleware
from matching_app.models import DATABASE_REVISION, DATABASE_SCHEMA
from matching_app.routes import router

logger = logging.getLogger("lostlink.matching")


def create_app(
    settings=None,
    *,
    token_verifier=None,
    database=None,
    internal_database=None,
    encoder=None,
    item_client=None,
):
    settings = settings or MatchingSettings()
    metrics = HttpMetrics(settings.service_name)

    @asynccontextmanager
    async def lifespan(application):
        configure_logging(settings.service_name, settings.log_level)
        owned = []
        try:
            for name, supplied in [
                ("database", database),
                ("internal_database", internal_database),
            ]:
                url = getattr(settings, f"{name}_url")
                if supplied is None and url:
                    db = MatchingDatabase(settings.model_copy(update={"database_url": url}))
                    setattr(application.state, name, db)
                    owned.append(db)
            yield
        finally:
            for db in owned:
                db.close()

    app = FastAPI(title="LostLink Matching Service", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.database, app.state.internal_database = database, internal_database
    app.state.encoder = encoder or ClipEncoder(
        settings.clip_checkpoint, settings.clip_sha256, settings.clip_device
    )
    app.state.item_client = item_client or make_item_client(settings)
    app.state.token_verifier = token_verifier or (
        CognitoVerifier(settings) if settings.cognito_issuer else None
    )
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
    install_error_handlers(app)
    install_matching_errors(app)
    app.include_router(router)

    @app.get("/health", tags=["Operations"])
    async def health():
        return {"status": "ok", "service": settings.service_name}

    def readiness_checks():
        checks = {
            "identity": bool(app.state.token_verifier),
            "clients": bool(settings.user_client_ids),
            "item": bool(app.state.item_client),
            "model": False,
        }
        try:
            app.state.encoder.ready()
            checks["model"] = True
        except Exception as exc:
            logger.warning("model_not_ready", extra={"error_type": type(exc).__name__})
        for name in ["database", "internal_database"]:
            try:
                db = getattr(app.state, name)
                if db is None:
                    raise RuntimeError("Database is not configured")
                db.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
                checks[name] = True
            except Exception as exc:
                checks[name] = False
                logger.warning("database_not_ready", extra={"error_type": type(exc).__name__})
        ok = all(checks.values())
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": checks},
            status_code=200 if ok else 503,
        )

    @app.get("/ready", tags=["Operations"])
    async def ready():
        # Probes must not queue behind synchronous searches in AnyIO's worker pool.
        return await asyncio.to_thread(readiness_checks)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus():
        return Response(
            generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    return app


app = create_app()
