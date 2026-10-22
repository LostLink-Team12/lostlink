# Application AWS configuration

This extends C1 for the backend contracts in `docs/team-alignment.md`. Enable it
with `application_runtime_enabled = true` in the ignored dev `terraform.tfvars`.
It does not install Helm releases, an ingress/load balancer, model weights, or CD.

## What is provisioned

- One encrypted domain SNS topic, three filtered SQS queues and three DLQs.
  Matching receives `item.registered`/`item.catalog_changed`; Item receives
  `item.embedded`/`item.embedding_failed`; Notification receives `match.found`.
  Raw delivery preserves the application's event envelope. Legacy C1 queues
  remain intact; deploy consumers using `application.queue_urls`, not `queue_urls`.
- Four confidential Cognito clients with disjoint, minimal receiver scopes.
  Owner App retains the public web authorization-code/PKCE client; Venue Portal
  uses a separate public operator client so Notification's allowlists are disjoint.
- Twenty separate database credential containers (15 runtime roles, five schema
  owner migration roles), one Claim crypto secret, and four machine secrets.
- Eighteen process-specific IRSA identities, scoped to service accounts with the
  same name in namespace `lostlink`. Only `item-tasks` can write `sealed/`;
  `item-api` uploads to `staging/`; `matching-tasks` can only read sealed photos.
  Item sealing can read a pinned source object version. Runtime roles cannot
  retrieve the RDS administrator secret or any migration credential.
- Optional SES sender and sandbox recipient identities. Verification links must
  be clicked in both mailboxes. No production-access request is needed for a demo
  between these verified identities.
- A Python 3.12 Cognito V2 trigger, private RDS connectivity, and a single-AZ
  Secrets Manager interface endpoint. There is no NAT gateway. The endpoint is
  deliberately not HA for this short demo and has an ongoing hourly charge.
  Secret containers also have an ongoing charge. Include both in cleanup.

The source-analysis baseline was generated with IAM Policy Autopilot 0.3.0 from
the actual backend modules. The deployed policies refine that baseline to actual
resources and process responsibilities; broad inferred S3 permissions are not
copied into the roles. AWS Access Analyzer and IAM simulation validate the final
policies. The six original C1 workload roles are legacy; C3 must use the new
`application.runtime_role_arns` process identities.

## Secrets and migration

Apply the Terraform plan before initializing values. Do not run
`scripts/init-local.sql` against AWS: its fixed password is for Compose only.

Use Python 3.12 and the pinned AWS SDK. Supply a full published commit SHA; the
bootstrap resolves it to ECR digests before starting jobs:

```powershell
uv run --no-project --python 3.12 --with boto3==1.43.115 python infra/terraform/scripts/bootstrap-app.py --kubectl infra/terraform/.terraform/tools/kubectl.exe --kubeconfig infra/terraform/.terraform/validation/kubeconfig --image-sha <published-full-commit-sha>
```

The operator requires AWS deployment access and an allowlisted EKS API address.
The script preserves existing secrets, generates unique random passwords and
Claim keys, and creates a restricted temporary namespace. A short-lived admin
job creates roles/schemas and enables pgvector. Five jobs run `alembic upgrade
head` with separate migration credentials. Only migration identities temporarily
receive database CREATE permission, because the existing Alembic environments
execute `CREATE SCHEMA IF NOT EXISTS`. The finalizer revokes it even if a
migration fails, then the namespace (including all temporary Secrets) is deleted.
Runtime roles never receive DDL privileges. Interrupted runs must be checked for
leftover `lostlink-bootstrap-*` namespaces and temporary migration CREATE grants.

Database secret JSON contains `username`, `password`, `host`, `port`, `dbname`.
C3 must build `postgresql+psycopg` URLs in memory, require `sslmode=verify-full`,
and mount the official RDS CA bundle. Use pool size 1 and max overflow 0 on the
small demo RDS. Inject only the credentials needed by each process. Bootstrap
passwords/Claim keys never enter Terraform state or local files. Cognito-generated
machine client secrets **are** present in encrypted remote Terraform state and
their managed secret versions; never publish state or plans.

Claim crypto JSON contains the three exact environment keys:
`FIELD_ENCRYPTION_KEYS` (JSON text), `FIELD_ENCRYPTION_ACTIVE_KEY_ID`, and
`HANDOVER_TOKEN_KEY`. Preserve old field-encryption keys when rotating. Bootstrap
reuses values and is not a rotation tool.

## Token trigger: build, test, then attach

Run `infra/terraform/scripts/build-token-trigger.ps1`. It installs locked Linux
Python 3.12 wheels, copies the existing Venue implementation and shared library,
adds the RDS CA and a Secrets Manager entry wrapper, and emits the zip path and
base64 SHA256. Build output lives under ignored `.terraform/` directories.

Set the resulting `token_trigger_package` and `token_trigger_sha256`, plus the
existing `token_trigger_pool_id`, `token_trigger_web_client_id` and
`token_trigger_operator_client_id`. Explicit IDs
avoid a Terraform dependency cycle through Cognito's trigger configuration.
Initially leave `attach_token_trigger = false`. Apply and directly test it:

```powershell
uv run --no-project --python 3.12 --with boto3==1.43.115 --with 'PyJWT[crypto]==2.15.1' python infra/terraform/scripts/verify-app.py --trigger-only
```

Only after success set `attach_token_trigger = true` and apply. The pool uses
Essentials and `V2_0`, customizing human access tokens without invoking the
directory for machine credentials. The Lambda delegates claim rules to
`venue_app.token_trigger`; it preserves trusted `platform_admin`, adds `owner`,
and removes stale venue roles/tenant claims when membership is absent. Failure
to read the directory fails closed. Its environment contains only secret ARN
and non-secret settings. The identity credential is refreshed every 60 seconds;
changed credentials dispose the old connection pool. Warm pools use one DB
connection. Account concurrency remains unreserved because the student account
must retain all ten unreserved executions; no concurrency increase is requested.

`platform_admin` membership is a trusted manual bootstrap action. Terraform does
not recreate the manually created administrator or change their password. A
`FORCE_CHANGE_PASSWORD` user must complete first-login password change. SES email
verification is separate from the user's Cognito `email_verified` attribute.

## C3 environment contract

Read `terraform output -json application` for public IDs, ARNs, queue URLs and
the single `event_topic_arn`. Values of secrets are not in that output.

| Receiver | Required client allowlist |
| --- | --- |
| Venue | `USER_CLIENT_IDS`: owner + operator; `PLATFORM_CLIENT_IDS`: operator; `MEMBER_CHECK_CLIENT_IDS`: Item + Claim |
| Item | `USER_CLIENT_IDS`: owner + operator; `CLAIM_CLIENT_IDS`: Claim; `INDEX_CLIENT_IDS`: Matching |
| Matching | `USER_CLIENT_IDS`: owner; `NOTIFICATION_CLIENT_IDS`: Notification |
| Claim | `USER_CLIENT_IDS`: owner + operator |
| Notification | `USER_CLIENT_IDS`: owner; `OPERATOR_CLIENT_IDS`: operator (these must be disjoint) |

Each service's `COGNITO_CLIENT_IDS` is the union of its allowed human and machine
client IDs; human and machine lists remain disjoint. Use the real Cognito issuer,
`custom:tenant_id`, and access tokens. Machine secrets supply `client_id`,
`client_secret`, `token_url`, `scopes`; map them to the backend's `MACHINE_*`
settings. Frontend `ownerClientId` uses `web_client_id`; `venueClientId` uses
`operator_client_id`. All public clients have no client secret.

Use `EVENT_TOPIC_ARN` on outbox publishers, the appropriate `EVENT_QUEUE_URL` on
consumers, and `COGNITO_USER_POOL_ID` on directory callers. Notification tasks use
`EMAIL_SENDER=ses`, the verified `SES_FROM_ADDRESS`, and the final Owner App HTTPS
base URL in `OWNER_APP_BASE_URL`. Container service URLs, frontend HTTPS origins,
external secret injection, model weights and ingress paths are C3 deployment
inputs, not credentials to guess here. Register the deployed entry paths with
trailing `/` in Cognito; current local paths are `/owner-app/` and `/venue-portal/`
on port 5173. S3 CORS permits POST from the exact configured origins.

For the combined live infrastructure check, keep business consumers paused and
the three development queues empty, then run `verify-app.py --probe-events` with
the same Python dependencies. It validates JWT signatures and scopes, validates
IAM policies, checks protected-secret denial and S3 boundaries, routes marked
probe messages and deletes only those messages. It never sends a human email.
The local JWT smoke test permits 30 seconds of workstation clock skew; backend
authentication is unchanged. Keep host/node clocks synchronized.

## Verification recorded on 2026-10-22

- All five real RDS schema migrations completed from published image digests.
- All 15 runtime roles passed TLS, non-owner/no-bypass/no-DDL, schema isolation,
  migration-version and forced-RLS checks. Temporary CREATE grants were revoked;
  all bootstrap namespaces were removed.
- All four real M2M access-token signatures/scopes passed; an Item request for a
  Claim scope was rejected.
- All 18 process policies passed Access Analyzer and protected-secret/S3 boundary
  checks. Five event types reached only their expected SQS queues; probes removed.
- Sender and demo recipient both report SES verification SUCCESS.
- Direct trigger invocation passed at 2,048 MiB: cold total 3,566.83 ms,
  warm 7.56 ms; stale venue claims were removed, trusted admin retained, and a
  machine client was rejected. Memory is billed only while invoked.

- Cognito reports Essentials with the deployed function attached as `V2_0`;
  SES sending is enabled in sandbox. The final real Terraform plan is unchanged.
- All 15 Terraform mock tests, Python static checks and formatting checks passed.
  The current source configuration passed Trivy HIGH/CRITICAL checks using its
  embedded bundle and the documented scoped C1 public-access exceptions.

Real user login and full application readiness remain C3 acceptance checks.
The administrator still needs to complete the first-login password change.
