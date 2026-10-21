# ADR-0007: Embedding runs as a worker process inside the Matching bounded context

Status: Accepted (21 Oct 2026). Supersedes the separate `embedding-worker` service in the proposal.

## Context
The proposal listed `embedding-worker` as its own service. During implementation the embedding
step turned out to share everything with Matching: the item vectors live in the `matching`
schema, search and auto-matching must use exactly the same CLIP checkpoint, and a new vector must
trigger re-matching of open lost reports in the same transaction.

## Decision
- Embedding is packaged in `services/matching-service` and deployed as separate processes from the
  same image: `api`, `consume` (SQS to job table), `tasks` (CLIP inference, vectors, outbox) and
  `outbox` (publishes events to SNS).
- Events keep `source = "embedding-worker"` for `item.embedded` and `item.embedding_failed`, so
  consumers and the event schema stay stable if the worker is split out later.
- One configured `MATCH_THRESHOLD` (initially 0.25) decides automatic matches. It must be
  calibrated on the demo dataset with `scripts/calibrate_match_threshold.py`; if text-only and
  photo queries need clearly different thresholds, we add per-modality thresholds.

## Consequences
+ One owner of the vector schema, one model version, no cross-schema writes.
+ API, consumer and tasks scale independently because they are separate Deployments.
- The `matching-service` image carries the ML dependencies; build a slim image without them for the
  consume and outbox processes if image size slows deployments.
- Search runs CLIP inference inside the API request, so API pods are CPU bound and need an HPA on CPU
  with enough headroom for the load test.
