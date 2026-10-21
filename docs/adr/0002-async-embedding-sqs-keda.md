# ADR-0002: Asynchronous embedding through SQS, scaled by KEDA

Status: Accepted (4 Oct 2026), amended 21 Oct 2026 (see below and ADR-0007)

## Context
CLIP embedding is CPU-heavy. A venue may upload thousands of items at once (e.g. after a large event), which must not slow down owner searches.

## Decision
item-service enqueues an embedding job in SQS. embedding-worker pods consume the queue. KEDA scales the worker Deployment on queue depth (scale to zero when idle). User-facing APIs scale separately with HPA on CPU.

## Consequences
+ Bursts are absorbed by the queue; this is also our scalability demo.
- Matching is eventually consistent: an item becomes searchable seconds after upload. The UI shows "processing".

## Amendment (21 Oct 2026)

The implementation keeps the decision (embedding is asynchronous and scales separately from the
APIs) but changes where the backlog lives:

- The SQS consumer (`matching_app.workers consume`) only persists each `item.registered` event as a
  `matching.work_jobs` row and deletes the message after commit. The CPU-heavy CLIP work is done by
  `matching_app.workers tasks`, which leases jobs from PostgreSQL (`FOR UPDATE SKIP LOCKED`).
- So SQS depth stays near zero during a bulk import, and scaling on it would never add workers.
  KEDA must scale the **tasks** Deployment with the `postgresql` scaler on the pending backlog:

  ```sql
  SELECT count(*) FROM matching.work_jobs
  WHERE state = 'pending' AND kind = 'embed' AND available_at <= now()
  ```

  run by a dedicated read-only role, target about 20 jobs per replica, `minReplicaCount: 1`
  (the API and the tasks worker must use the same pinned checkpoint, and cold starts load a
  ~350 MB model), `maxReplicaCount` bounded by the RDS connection budget.
- The consume Deployment stays small (1-2 replicas) and can still use the SQS scaler.

Why the change is better: the job table gives retries with backoff, leases, supersession by newer item
versions and an audit trail without a second queue, and a job can never be lost between SQS and the
database. The cost is that the scaling signal now needs database access; see ADR-0007.
