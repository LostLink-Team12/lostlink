# ADR-0004: Domain events on SNS, delivered to partners as webhooks

Status: Accepted (4 Oct 2026)

## Context
Partners (couriers, insurers) and new seeds must react to platform events without changes to core services.

## Decision
Services publish versioned domain events to SNS topics. Internal consumers subscribe through SQS queues. notification-service forwards subscribed events to partner webhooks, signed with HMAC.

## Consequences
+ New partners and seeds are added by subscription, not by code changes in producers.
- Requires an event schema registry (`libs/lostlink_common.events`) and idempotent consumers.
