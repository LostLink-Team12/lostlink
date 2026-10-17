# ADR-0002: Asynchronous embedding through SQS, scaled by KEDA

Status: Accepted (4 Oct 2026)

## Context
CLIP embedding is CPU-heavy. A venue may upload thousands of items at once (e.g. after a large event), which must not slow down owner searches.

## Decision
item-service enqueues an embedding job in SQS. embedding-worker pods consume the queue. KEDA scales the worker Deployment on queue depth (scale to zero when idle). User-facing APIs scale separately with HPA on CPU.

## Consequences
+ Bursts are absorbed by the queue; this is also our scalability demo.
- Matching is eventually consistent: an item becomes searchable seconds after upload. The UI shows "processing".
