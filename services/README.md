# Services

One FastAPI microservice per bounded context. Start every new service by copying `service-template/`.

[Venue Service](venue-service/README.md) implements UC1 onboarding, staff roles,
current membership checks, and the Cognito token-trigger adapter.
[Item Service](item-service/README.md) implements UC2 registration, immutable photos,
bulk imports and the reservation API for Claim. Other business
services in the table remain planned unless their implementation directories exist.

[Matching Service](matching-service/README.md) implements UC3 owner reports,
cross-venue text/photo retrieval, and an independently running Embedding worker.
The worker shares the Matching bounded context and schema; it is packaged in
`matching-service`, rather than a second service directory.

[Claim Service](claim-service/README.md) implements UC5 claims with verification
answers, venue approval coordinated with Item reservations, encrypted contact
disclosure and single-use QR handover.

[Notification Service](notification-service/README.md) implements the UC4 email
alert: it consumes match.found, revalidates the match with Matching, resolves the
owner's verified email and sends through SES with explicit unknown-outcome handling.

[Delivery Service](delivery-service/README.md) is the Seed 2 skeleton: the shipment
lifecycle and its reactions to claim events, ahead of the Phase 2 Partner API (ADR-0009).

| Service | Bounded context | Use cases | Owner | Phase |
| --- | --- | --- | --- | --- |
| venue-service | Venue and Tenant | UC1 | JIANG YIBAI | 1 |
| item-service | Item Catalogue | UC2 | JIANG YIBAI | 1 |
| claim-service | Claim and Verification | UC5 | JIANG YIBAI | 1 |
| notification-service | Notification (partner webhooks in Phase 2) | UC4, UC6 | JIANG YIBAI | 1 |
| matching-service | Matching, plus the Embedding worker processes (ADR-0007) | UC3, UC4 | YANG DONGWEI (design), JIANG YIBAI (implementation) | 1 |
| delivery-service | Return Delivery (Seed 2), lifecycle skeleton | UC6 | YANG DONGWEI | 2 |
| analytics-service | Analytics | UC7 | ZHANG JI | 2 |
