# lostlink-common

Owner: JIANG YIBAI. Installable Python 3.12 package shared by LostLink services.

| Module | Responsibility |
| --- | --- |
| `config` | Validated environment configuration; secrets masked in representations |
| `auth` | Cognito RS256 access tokens, JWKS rotation, client allowlist, roles, scopes, tenant context |
| `db` | PostgreSQL sessions, transaction-local tenant ID, RLS migration helper, runtime-role checks |
| `event_schemas` | Version-one payload registry for the five initial domain events |
| `events` | Validated envelope, SNS/SQS publishers, raw and SNS-wrapped message decoding |
| `observability` | JSON application/access logs, request IDs, route-based Prometheus metrics |
| `errors` | Uniform API errors without SQL, token, or request-body disclosure |

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

Architecture and handoff decisions are recorded in
[the backend foundation](../docs/backend-foundation.md) and
[team alignment](../docs/team-alignment.md).
