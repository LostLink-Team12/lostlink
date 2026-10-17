# ADR-0001: One microservice per bounded context

Status: Accepted (4 Oct 2026)

## Context
Four members build in parallel; new seeds (return delivery, events, hotels) must be added without changing the core.

## Decision
Each bounded context is a separately deployable FastAPI service with its own schema. Services talk synchronously over REST for queries and asynchronously through SNS/SQS events for state changes.

## Consequences
+ Clear ownership, independent scaling (e.g. embedding workers), seeds plug in through APIs and events.
- More moving parts to deploy and observe; mitigated by the service template, Helm charts and one CI pipeline.
