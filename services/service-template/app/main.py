"""Golden-path service template for LostLink.

Copy this folder to services/<name> to start a new service.
Every service exposes /health (liveness) and /ready (readiness) for Kubernetes probes.
"""
import os

from fastapi import FastAPI

SERVICE_NAME = os.getenv("SERVICE_NAME", "service-template")

app = FastAPI(title=SERVICE_NAME, version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/ready")
def ready() -> dict:
    # Replace with real dependency checks (database, queue) in each service.
    return {"status": "ready"}
