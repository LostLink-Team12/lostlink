"""Delivery HTTP lifecycle; the claim.* consumer and the outbox publisher are worker processes."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from lostlink_common.auth import CognitoVerifier
from lostlink_common.crypto import FieldCipher
from lostlink_common.db import Database
from lostlink_common.errors import install_error_handlers
from lostlink_common.observability import HttpMetrics, ObservabilityMiddleware, configure_logging
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from delivery_app.config import DeliverySettings
from delivery_app.domain import TERMINAL, TRANSITIONS
from delivery_app.errors import install_delivery_errors
from delivery_app.models import DATABASE_REVISION, DATABASE_SCHEMA
from delivery_app.routes import router

logger = logging.getLogger("lostlink.delivery")
UNSET = object()


def lifecycle() -> dict:
    """Published state machine so the Owner App and partners render the same states."""
    return {
        "transitions": [
            {"from": src, "to": dst, "actors": sorted(actors)}
            for (src, dst), actors in TRANSITIONS.items()
        ],
        "terminal": sorted(TERMINAL),
    }


def create_app(settings=None, *, token_verifier=None, database=None, cipher=UNSET):
    settings = settings or DeliverySettings()
    metrics = HttpMetrics(settings.service_name)

    @asynccontextmanager
    async def lifespan(application):
        configure_logging(settings.service_name, settings.log_level)
        owned = None
        try:
            if application.state.database is None and settings.database_url:
                owned = application.state.database = Database(settings)
            yield
        finally:
            if owned:
                owned.close()

    app = FastAPI(title="LostLink Delivery Service", version="0.2.0", lifespan=lifespan)
    app.state.settings, app.state.database = settings, database
    app.state.cipher = (
        FieldCipher.from_settings(
            settings.field_encryption_keys, settings.field_encryption_active_key_id
        )
        if cipher is UNSET
        else cipher
    )
    app.state.token_verifier = token_verifier or (
        CognitoVerifier(settings) if settings.cognito_issuer else None
    )
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
    install_error_handlers(app)
    install_delivery_errors(app)
    app.include_router(router)

    @app.get("/health", tags=["Operations"])
    def health() -> dict:
        return {"status": "ok", "service": settings.service_name}

    @app.get("/ready", tags=["Operations"])
    def ready():
        checks = {
            "identity": bool(app.state.token_verifier),
            "clients": bool(settings.user_client_ids and settings.partner_client_ids),
            "encryption": bool(app.state.cipher),
        }
        try:
            if app.state.database is None:
                raise RuntimeError("Database is not configured")
            app.state.database.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
            checks["database"] = True
        except Exception as exc:
            checks["database"] = False
            logger.warning("delivery_readiness_failed", extra={"error_type": type(exc).__name__})
        ok = all(checks.values())
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": checks},
            status_code=200 if ok else 503,
        )

    @app.get("/v1/shipment-lifecycle", tags=["Lifecycle"])
    def shipment_lifecycle() -> dict:
        return lifecycle()

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint():
        return Response(
            generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    return app


app = create_app()
