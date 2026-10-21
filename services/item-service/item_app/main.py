"""Item HTTP lifecycle; no in-process durable background work."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from lostlink_common.auth import CognitoVerifier
from lostlink_common.errors import install_error_handlers
from lostlink_common.membership import ClientCredentials, MembershipClient
from lostlink_common.observability import HttpMetrics, ObservabilityMiddleware, configure_logging
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from item_app.config import ItemSettings
from item_app.database import ItemDatabase
from item_app.errors import install_item_errors
from item_app.models import DATABASE_REVISION, DATABASE_SCHEMA
from item_app.routes import router
from item_app.storage import PhotoStorage, s3_client

logger = logging.getLogger("lostlink.item")


def create_app(
    settings=None,
    *,
    token_verifier=None,
    database=None,
    internal_database=None,
    membership_client=None,
    storage=None,
):
    settings = settings or ItemSettings()
    metrics = HttpMetrics(settings.service_name)

    @asynccontextmanager
    async def lifespan(application):
        configure_logging(settings.service_name, settings.log_level)
        owned, client = [], None
        try:
            for name, supplied in [
                ("database", database),
                ("internal_database", internal_database),
            ]:
                url = getattr(settings, f"{name}_url")
                if supplied is None and url:
                    db = ItemDatabase(settings.model_copy(update={"database_url": url}))
                    setattr(application.state, name, db)
                    owned.append(db)
            if storage is None and settings.photo_bucket:
                client = s3_client(settings)
                application.state.storage = PhotoStorage(client)
            yield
        finally:
            for db in owned:
                db.close()
            if client:
                client.close()

    app = FastAPI(title="LostLink Item Service", version="0.1.0", lifespan=lifespan)
    app.state.settings, app.state.storage = settings, storage
    app.state.database, app.state.internal_database = database, internal_database
    app.state.membership_client = membership_client
    if membership_client is None and all(
        [
            settings.venue_base_url,
            settings.machine_token_url,
            settings.machine_client_id,
            settings.machine_client_secret,
        ]
    ):
        provider = ClientCredentials(
            settings.machine_token_url,
            settings.machine_client_id,
            settings.machine_client_secret.get_secret_value(),
            ["lostlink/members.check"],
        )
        app.state.membership_client = MembershipClient(settings.venue_base_url, provider)
    app.state.token_verifier = token_verifier or (
        CognitoVerifier(settings) if settings.cognito_issuer else None
    )
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
    install_error_handlers(app)
    install_item_errors(app)
    app.include_router(router)

    @app.get("/health", tags=["Operations"])
    def health():
        return {"status": "ok", "service": settings.service_name}

    @app.get("/ready", tags=["Operations"])
    def ready():
        checks = {
            "identity": bool(app.state.token_verifier),
            "membership": bool(app.state.membership_client),
            "clients": bool(settings.user_client_ids),
            "storage": False,
        }
        for name in ["database", "internal_database"]:
            try:
                db = getattr(app.state, name)
                if db is None:
                    raise RuntimeError("Database is not configured")
                db.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
                checks[name] = True
            except Exception as exc:
                checks[name] = False
                logger.warning("item_readiness_failed", extra={"error_type": type(exc).__name__})
        if app.state.storage and settings.photo_bucket:
            try:
                app.state.storage.ready(settings.photo_bucket)
                checks["storage"] = True
            except Exception as exc:
                logger.warning("storage_readiness_failed", extra={"error_type": type(exc).__name__})
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
