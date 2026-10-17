# ADR-0003: pgvector inside RDS PostgreSQL instead of a dedicated vector database

Status: Accepted (4 Oct 2026)

## Context
We need similarity search over item embeddings, joined with relational filters (venue, category, date, status).

## Decision
Store embeddings in a `vector(512)` column in RDS PostgreSQL with an HNSW index.

## Consequences
+ One managed store, transactional consistency with item data, SQL filters in the same query, lower cost.
- Lower ceiling than a dedicated engine (e.g. OpenSearch k-NN) at very large scale; recorded as an architectural limitation with a migration path.
