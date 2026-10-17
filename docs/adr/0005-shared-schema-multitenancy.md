# ADR-0005: Shared-schema multi-tenancy with row-level security

Status: Accepted (4 Oct 2026)

## Context
Each venue is a tenant. Onboarding must be cheap, and venues must never see each other's items or owners' contact details.

## Decision
One database schema per service, a `tenant_id` column on tenant data, PostgreSQL row-level security, and the tenant taken from the Cognito JWT.

## Consequences
+ Adding a venue is a row, not new infrastructure.
- A bug in tenant scoping is a data leak; mitigated by RLS in the database and tests for cross-tenant access.
