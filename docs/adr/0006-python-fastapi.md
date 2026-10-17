# ADR-0006: Python 3.12 and FastAPI for all services

Status: Proposed (17 Oct 2026), confirm at kick-off

## Context
The AI part (CLIP) is Python. The team has 9 days to the presentation; one language keeps the template, shared libraries and CI simple.

## Decision
All backend services use Python 3.12, FastAPI, SQLAlchemy and Alembic, packaged as Docker images from `services/service-template`.

## Consequences
+ One toolchain, shared libraries, fast development, OpenAPI docs generated automatically.
- Python is slower per request than Go or Java; we scale horizontally with HPA instead.
