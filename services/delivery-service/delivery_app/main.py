"""Delivery HTTP lifecycle. Phase 2 adds persistence, the consumer and partner routes."""

from fastapi import FastAPI

from delivery_app.domain import TERMINAL, TRANSITIONS

SERVICE_NAME = "delivery-service"


def create_app() -> FastAPI:
    app = FastAPI(title=SERVICE_NAME, version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": SERVICE_NAME}

    @app.get("/ready")
    def ready() -> dict:
        # No external dependency yet; becomes a database and queue check in Phase 2.
        return {"status": "ready"}

    @app.get("/v1/shipment-lifecycle")
    def lifecycle() -> dict:
        """Published state machine so the Owner App and partners render the same states."""
        return {
            "transitions": [
                {"from": src, "to": dst, "actors": sorted(actors)}
                for (src, dst), actors in TRANSITIONS.items()
            ],
            "terminal": sorted(TERMINAL),
        }

    return app


app = create_app()
