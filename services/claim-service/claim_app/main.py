"""Claim HTTP lifecycle; durable coordination runs only in separate worker processes."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from lostlink_common.auth import CognitoVerifier
from lostlink_common.crypto import FieldCipher
from lostlink_common.db import Database
from lostlink_common.errors import install_error_handlers
from lostlink_common.events import aws_client
from lostlink_common.identity import CognitoIdentityDirectory
from lostlink_common.membership import MembershipClient
from lostlink_common.observability import HttpMetrics, ObservabilityMiddleware, configure_logging
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from claim_app.clients import make_item_client, make_token_provider
from claim_app.config import ClaimSettings
from claim_app.errors import install_claim_errors
from claim_app.models import DATABASE_REVISION, DATABASE_SCHEMA
from claim_app.routes import router

logger = logging.getLogger("lostlink.claim")
UNSET = object()


def create_app(
    settings=None,
    *,
    token_verifier=None,
    database=None,
    membership_client=None,
    item_client=None,
    identity_directory=None,
    cipher=UNSET,
    token_key=UNSET,
):
    settings = settings or ClaimSettings()
    metrics = HttpMetrics(settings.service_name)
    token_provider = make_token_provider(settings)

    @asynccontextmanager
    async def lifespan(application):
        configure_logging(settings.service_name, settings.log_level)
        owned, cognito = None, None
        try:
            if database is None and settings.database_url:
                owned = Database(settings)
                application.state.database = owned
            if identity_directory is None and settings.cognito_user_pool_id:
                cognito = aws_client("cognito-idp", settings)
                application.state.identity_directory = CognitoIdentityDirectory(
                    cognito, settings.cognito_user_pool_id
                )
            yield
        finally:
            if owned:
                owned.close()
            if cognito:
                cognito.close()

    app = FastAPI(title="LostLink Claim Service", version="0.1.0", lifespan=lifespan)
    app.state.settings, app.state.database = settings, database
    app.state.identity_directory = identity_directory
    app.state.membership_client = membership_client
    if membership_client is None and settings.venue_base_url and token_provider:
        app.state.membership_client = MembershipClient(settings.venue_base_url, token_provider)
    app.state.item_client = item_client or make_item_client(settings, token_provider)
    app.state.cipher = (
        FieldCipher.from_settings(
            settings.field_encryption_keys, settings.field_encryption_active_key_id
        )
        if cipher is UNSET
        else cipher
    )
    app.state.token_key = settings.token_key_bytes() if token_key is UNSET else token_key
    app.state.token_verifier = token_verifier or (
        CognitoVerifier(settings) if settings.cognito_issuer else None
    )
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
    install_error_handlers(app)
    install_claim_errors(app)
    app.include_router(router)

    @app.get("/health", tags=["Operations"])
    def health():
        return {"status": "ok", "service": settings.service_name}

    @app.get("/ready", tags=["Operations"])
    def ready():
        state = app.state
        checks = {
            "identity": bool(state.token_verifier),
            "clients": bool(settings.user_client_ids),
            "membership": bool(state.membership_client),
            "item": bool(state.item_client),
            "identity_directory": bool(state.identity_directory),
            "encryption": bool(state.cipher),
            "handover_key": bool(state.token_key),
        }
        try:
            if state.database is None:
                raise RuntimeError("Database is not configured")
            state.database.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
            checks["database"] = True
        except Exception as exc:
            checks["database"] = False
            logger.warning("claim_readiness_failed", extra={"error_type": type(exc).__name__})
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
