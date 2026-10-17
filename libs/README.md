# Shared libraries (owner: JIANG YIBAI)

Planned in Phase 1:

- `lostlink_common.auth` - verify Cognito JWTs, extract user, role and tenant (venue) ID
- `lostlink_common.events` - domain event envelope and SNS/SQS publisher (`item.registered`, `item.embedded`, `lost_report.created`, `match.found`, `claim.approved`)
- `lostlink_common.db` - SQLAlchemy session, tenant-scoped queries
- `lostlink_common.observability` - structured logging, Prometheus metrics middleware
