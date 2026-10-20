# lostlink-common

Owner: JIANG YIBAI. Installable Python 3.12 package shared by LostLink services.

| Module | Responsibility |
| --- | --- |
| `config` | Validated environment configuration; secrets masked in representations |
| `auth` | Cognito RS256 access tokens, JWKS rotation, client allowlist, roles, scopes, tenant context, subject validation |
| `db` | PostgreSQL sessions with transaction-local context: tenant (`session`), explicit `scoped_session`, owner/venue `actor_session`, `system_session`; `get_owner_session` dependency; tenant and owner-or-tenant RLS migration helpers; runtime-role checks |
| `event_schemas` | Ten version-one events: initial five, membership changes, item catalogue snapshots, embedding failures, claim handover and claim cancellation |
| `events` | Validated envelope, SNS/SQS publishers, raw and SNS-wrapped message decoding |
| `observability` | JSON application/access logs, request IDs, route-based Prometheus metrics |
| `errors` | Uniform API errors without SQL, token, or request-body disclosure |
| `membership` | Cached client-credentials tokens and uncached, fail-closed current Venue membership checks |
| `crypto` | AES-256-GCM field encryption with key IDs, rotation and associated data bound to service, table, column and record ID |
| `identity` | Verified-email lookup by exact subject: Cognito `ListUsers` adapter and a test-only static double |

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.lock -e ./libs
pytest -q -m "not integration"
ruff check .
ruff format --check .
```

On Windows, activate with `.venv\Scripts\Activate.ps1`.
Each service owns its models, migrations, policies, and domain rules. The shared
package contains no service-owned ORM tables. Use the service template as the
integration example.

### Access contexts

- `Database.session(tenant_id)` keeps the original venue-tenant behaviour.
- `Database.actor_session(principal, venue_scope=...)` must state the mode: owner
  mode sets only `app.user_sub`; venue mode also sets the verified tenant.
- `Database.scoped_session(subject=..., tenant_id=..., resource_id=...)` is the
  explicit low-level form for trusted resource locators and trusted events.
- `Database.system_session()` sets no actor; RLS tables return nothing and the
  worker role's grants define what operational tables it may use.
- `enable_owner_or_tenant_rls` ORs an owner policy with a tenant policy. Restrict
  writable columns with grants and reject self-review in the service.

### Field encryption and identity

Configure encryption keys from a secret as a JSON object of key ID to base64
32-byte key plus the active key ID; `FieldCipher.from_settings` returns `None`
when both are absent and fails on a partial configuration. Store the returned
`key_id` beside the ciphertext. `CognitoIdentityDirectory` needs only
`cognito-idp:ListUsers` on one user pool; `StaticIdentityDirectory` is for tests
and local runs only.

Runtime dependencies are pinned in `requirements.lock`; test tools are pinned in
`requirements-dev.lock`. Regenerate both together after a dependency change:

```bash
uv pip compile libs/pyproject.toml services/service-template/requirements.in --python-version 3.12 --universal -o requirements.lock
uv pip compile requirements-dev.in --python-version 3.12 --universal -o requirements-dev.lock
```

To exercise PostgreSQL and LocalStack, start the root Compose stack and set:

```powershell
$env:LOSTLINK_TEST_ADMIN_URL = "postgresql+psycopg://lostlink:lostlink@localhost:5432/lostlink"
$env:LOSTLINK_TEST_AWS_ENDPOINT = "http://localhost:4566"
python -m pytest -q -m integration
```

Use a local development administrator: the suite creates and removes a randomly
named test database, role, SNS topic, and SQS queue. It does not migrate or clear
the database named in the administrator URL. Tests are skipped when the respective
integration environment variable is absent; CI configures both.

Integration conventions and per-member requirements are in
[the backend integration guide](../docs/team-alignment.md); generated contracts,
including the event schemas, are in [docs/api](../docs/api/README.md).
