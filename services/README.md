# Services

One FastAPI microservice per bounded context. Start every new service by copying `service-template/`.

[Venue Service](venue-service/README.md) implements UC1 onboarding, staff roles,
current membership checks, and the Cognito token-trigger adapter.
[Item Service](item-service/README.md) implements UC2 registration, immutable photos,
bulk imports and the reservation API for Claim. Other business
services in the table remain planned unless their implementation directories exist.

| Service | Bounded context | Use cases | Owner | Phase |
| --- | --- | --- | --- | --- |
| venue-service | Venue and Tenant | UC1 | JIANG YIBAI | 1 |
| item-service | Item Catalogue | UC2 | JIANG YIBAI | 1 |
| claim-service | Claim and Verification | UC5 | JIANG YIBAI | 1 |
| notification-service | Notification, partner webhooks | UC4, UC6 | JIANG YIBAI | 1 |
| embedding-worker | Media and Embedding (SQS consumer) | UC2, UC3 | YANG DONGWEI | 1 |
| matching-service | Matching | UC3, UC4 | YANG DONGWEI | 1 |
| delivery-service | Return Delivery (Seed 2) | UC6 | YANG DONGWEI | 2 |
| analytics-service | Analytics | UC7 | ZHANG JI | 2 |
