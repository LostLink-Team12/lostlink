"""Real PostgreSQL, independent login roles and disposable test-owned resources."""

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from lostlink_common.auth import InvalidToken, Principal
from lostlink_common.config import Settings
from lostlink_common.events import DomainEvent, SnsPublisher, aws_client, decode_message
from lostlink_common.membership import MembershipClient
from psycopg import sql
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from venue_app import token_trigger
from venue_app import workers as venue_workers
from venue_app.config import VenueSettings
from venue_app.database import VenueDatabase
from venue_app.main import create_app
from venue_app.models import DATABASE_REVISION, IdentityJob, Membership, OutboxEvent
from venue_app.service import resolve_identity
from venue_app.workers import JobRunner

pytestmark = pytest.mark.integration


@pytest.fixture
def env():
    admin_url = os.getenv("LOSTLINK_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set LOSTLINK_TEST_ADMIN_URL to a local PostgreSQL administrator URL")
    base = make_url(admin_url)
    suffix = uuid4().hex
    db_name = f"ll_venue_test_{suffix}"
    roles = {mode: f"ll_v_{mode}_{suffix}" for mode in ["api", "platform", "worker", "identity"]}
    password = uuid4().hex
    admin_dsn = base.set(drivername="postgresql").render_as_string(hide_password=False)
    databases, created_roles = {}, []
    created_database = False
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        try:
            for role in roles.values():
                admin.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER "
                        "NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
                created_roles.append(role)
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
            created_database = True
            migration_url = base.set(database=db_name).render_as_string(hide_password=False)
            cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("MIGRATION_DATABASE_URL", migration_url)
                for mode, role in roles.items():
                    patch.setenv(f"VENUE_{mode.upper()}_ROLE", role)
                command.upgrade(cfg, "head")
                command.upgrade(cfg, "head")
            for mode, role in roles.items():
                url = base.set(database=db_name, username=role, password=password)
                databases[mode] = VenueDatabase(
                    Settings(
                        _env_file=None,
                        database_url=url.render_as_string(hide_password=False),
                        db_pool_size=4,
                    )
                )
            identities = {
                "ops": Principal(subject="operator", client_id="ops", roles={"platform_admin"}),
                "item": Principal(
                    subject="item-machine", client_id="item", scopes={"lostlink/members.check"}
                ),
                "identity": Principal(
                    subject="trigger", client_id="trigger", scopes={"lostlink/identity.resolve"}
                ),
                "owner": Principal(subject="owner", client_id="web", roles={"owner"}),
            }

            class Verifier:
                def verify(self, token):
                    if token not in identities:
                        raise InvalidToken()
                    return identities[token]

            settings = VenueSettings(
                _env_file=None,
                user_client_ids=["web"],
                platform_client_ids=["ops"],
                member_check_client_ids=["item"],
                identity_resolve_client_ids=["trigger"],
            )
            app = create_app(
                settings,
                token_verifier=Verifier(),
                database=databases["api"],
                platform_database=databases["platform"],
                identity_database=databases["identity"],
            )
            with TestClient(app) as client:
                yield SimpleNamespace(
                    db=databases,
                    client=client,
                    identities=identities,
                    migration_url=migration_url,
                    cfg=cfg,
                    roles=roles,
                )
        finally:
            for database in databases.values():
                database.close()
            if created_database:
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(db_name)))
            for role in created_roles:
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def headers(actor="ops", key=None, version=None):
    result = {"Authorization": f"Bearer {actor}", "Idempotency-Key": key or uuid4().hex}
    if version is not None:
        result["If-Match"] = f'"{version}"'
    return result


def onboard(env, name="a"):
    response = env.client.post(
        "/v1/admin/venues",
        headers=headers(),
        json={"name": f"Venue {name}", "slug": name, "initial_admin_sub": f"admin-{name}"},
    )
    assert response.status_code == 201, response.text
    data = response.json()
    tenant = UUID(data["venue"]["id"])
    env.identities[f"admin-{name}"] = Principal(
        subject=f"admin-{name}", client_id="web", tenant_id=tenant, roles={"venue_admin"}
    )
    return data


def add_member(env, subject="staff-a", role="venue_staff", actor="admin-a"):
    response = env.client.post(
        "/v1/venues/current/members",
        headers=headers(actor),
        json={"user_sub": subject, "role": role},
    )
    assert response.status_code == 201, response.text
    env.identities[subject] = Principal(
        subject=subject, client_id="web", roles={role}, tenant_id=env.identities[actor].tenant_id
    )
    return response.json()


def token_event(subject, *, client="web", pool="pool", version="2"):
    return {
        "version": version,
        "userPoolId": pool,
        "triggerSource": "TokenGeneration_Authentication",
        "callerContext": {"clientId": client},
        "request": {
            "userAttributes": {"sub": subject, "custom:tenant_id": str(uuid4())},
            "groupConfiguration": {"groupsToOverride": ["venue_admin", "unrelated"]},
        },
        "response": {},
    }


@pytest.fixture(params=["python", "lambda_zip"])
def invoke_token_trigger(request, env, monkeypatch, tmp_path):
    config = {
        "ENVIRONMENT": "dev",
        "IDENTITY_DATABASE_URL": env.db["identity"].engine.url.render_as_string(
            hide_password=False
        ),
        "COGNITO_USER_POOL_ID": "pool",
        "USER_CLIENT_IDS": '["web"]',
        "PLATFORM_CLIENT_IDS": '["ops"]',
        "DB_POOL_SIZE": "1",
        "DB_MAX_OVERFLOW": "0",
        "DB_STATEMENT_TIMEOUT_MS": "500",
    }
    if request.param == "python":
        for field in VenueSettings.model_fields:
            monkeypatch.delenv(field.upper(), raising=False)
        for key, value in config.items():
            monkeypatch.setenv(key, value)
        token_trigger.dependencies.cache_clear()
        try:
            yield lambda event: token_trigger.handler(event, None)
        finally:
            if token_trigger.dependencies.cache_info().currsize:
                token_trigger.dependencies()[1].close()
            token_trigger.dependencies.cache_clear()
        return

    artifact = os.getenv("LOSTLINK_TEST_TRIGGER_ARTIFACT")
    if not artifact:
        pytest.skip("Build the Lambda ZIP and set LOSTLINK_TEST_TRIGGER_ARTIFACT")
    archive_path = Path(artifact).resolve()
    manifest = json.loads(archive_path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    with archive_path.open("rb") as content:
        assert hashlib.file_digest(content, "sha256").hexdigest() == manifest["zip_sha256"]
    assert manifest["handler"] == "venue_app.token_trigger.handler"
    assert (manifest["runtime"], manifest["architecture"]) == ("python3.12", "x86_64")
    with zipfile.ZipFile(archive_path) as archive:
        assert "venue_app/token_trigger.py" in archive.namelist()
        installed = archive.read("requirements.txt").decode()
        assert "psycopg-binary==" in installed and "pydantic-core==" in installed
        assert (
            hashlib.sha256(archive.read("rds-global-bundle.pem")).hexdigest()
            == manifest["rds_ca_sha256"]
        )
        assert sum(entry.file_size for entry in archive.infolist()) <= 250 * 1024 * 1024
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            assert not path.is_absolute() and ".." not in path.parts
            assert path.suffix not in {".pyc", ".pyd"} and path.name != ".env"
    # Only the generated ZIP is copied, without the checkout or host Python packages.
    url = make_url(config["IDENTITY_DATABASE_URL"])
    config["IDENTITY_DATABASE_URL"] = url.set(host="host.docker.internal").render_as_string(
        hide_password=False
    )
    env_file = tmp_path / "trigger.env"
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in config.items()), encoding="utf-8"
    )
    env_file.chmod(0o600)
    name = f"lostlink-trigger-test-{uuid4().hex}"
    subprocess.run(
        [
            "docker",
            "create",
            "--rm",
            "--name",
            name,
            "--platform",
            "linux/amd64",
            "--publish",
            "127.0.0.1::8080",
            "--add-host",
            "host.docker.internal:host-gateway",
            "--env-file",
            str(env_file),
            "--entrypoint",
            "/bin/sh",
            "public.ecr.aws/lambda/python:3.12",
            "-c",
            "python -m zipfile -e /tmp/function.zip /var/task "
            "&& exec /lambda-entrypoint.sh venue_app.token_trigger.handler",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        subprocess.run(
            ["docker", "cp", str(archive_path), f"{name}:/tmp/function.zip"],
            check=True,
            capture_output=True,
        )
        subprocess.run(["docker", "start", name], check=True, capture_output=True)
        port = subprocess.check_output(["docker", "port", name, "8080/tcp"], text=True).strip()
        endpoint = (
            f"http://127.0.0.1:{port.rsplit(':', 1)[1]}/2015-03-31/functions/function/invocations"
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def invoke(event):
            deadline = time.monotonic() + 30
            while True:
                try:
                    with opener.open(
                        urllib.request.Request(
                            endpoint,
                            data=json.dumps(event).encode(),
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        ),
                        timeout=10,
                    ) as response:
                        result = json.load(response)
                    break
                except (urllib.error.URLError, ConnectionError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
            if "errorType" in result:
                raise RuntimeError(result["errorMessage"])
            return result

        yield invoke
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=True, capture_output=True)


def token_identity(result):
    override = result["response"]["claimsAndScopeOverrideDetails"]
    access = override["accessTokenGeneration"]
    return (
        set(override["groupOverrideDetails"]["groupsToOverride"]),
        access["claimsToAddOrOverride"],
        access["claimsToSuppress"],
    )


def test_token_trigger_entry_reads_current_membership_and_isolates_warm_invocations(
    env, invoke_token_trigger
):
    a, b = onboard(env, "a"), onboard(env, "b")
    member = add_member(env)
    for subject, role, tenant in [
        ("staff-a", "venue_staff", a["venue"]["id"]),
        ("owner", None, None),
        ("admin-b", "venue_admin", b["venue"]["id"]),
        ("staff-a", "venue_staff", a["venue"]["id"]),
    ]:
        groups, additions, suppressed = token_identity(invoke_token_trigger(token_event(subject)))
        assert groups == {"owner", "unrelated"} | ({role} if role else set())
        assert additions == ({"custom:tenant_id": tenant} if tenant else {})
        assert suppressed == ([] if tenant else ["custom:tenant_id"])
    promoted = env.client.patch(
        f"/v1/venues/current/members/{member['id']}",
        headers=headers("admin-a", version=1),
        json={"role": "venue_admin"},
    )
    assert promoted.status_code == 200
    assert "venue_admin" in token_identity(invoke_token_trigger(token_event("staff-a")))[0]
    suspended = env.client.patch(
        f"/v1/venues/current/members/{member['id']}",
        headers=headers("admin-a", version=2),
        json={"status": "suspended"},
    )
    assert suspended.status_code == 200
    assert token_identity(invoke_token_trigger(token_event("staff-a"))) == (
        {"owner", "unrelated"},
        {},
        ["custom:tenant_id"],
    )
    response = env.client.patch(
        f"/v1/admin/venues/{b['venue']['id']}",
        headers=headers(version=1),
        json={"status": "suspended"},
    )
    assert response.status_code == 200
    assert token_identity(invoke_token_trigger(token_event("admin-b")))[1] == {}
    assert (
        "platform_admin"
        not in token_identity(invoke_token_trigger(token_event("owner", client="ops")))[0]
    )


@pytest.mark.parametrize("change", [{"client": "unknown"}, {"pool": "other"}, {"version": "1"}])
def test_token_trigger_entry_rejects_invalid_invocations(env, invoke_token_trigger, change):
    onboard(env)
    with pytest.raises(RuntimeError, match="^Token customization unavailable$"):
        invoke_token_trigger(token_event("admin-a", **change))


def test_token_trigger_entry_database_failure_cannot_reuse_previous_claims(
    env, invoke_token_trigger
):
    onboard(env)
    assert token_identity(invoke_token_trigger(token_event("admin-a")))[1]
    engine = create_engine(env.migration_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("LOCK TABLE venue.memberships IN ACCESS EXCLUSIVE MODE"))
            machine = {"version": "3", "triggerSource": "TokenGeneration_ClientCredentials"}
            assert invoke_token_trigger(machine) == machine
            with pytest.raises(RuntimeError, match="^Token customization unavailable$"):
                invoke_token_trigger(token_event("admin-a"))
        # The failed transaction must roll back; the warm process must recover.
        assert token_identity(invoke_token_trigger(token_event("admin-a")))[1]
        role = env.roles["identity"]  # Test-owned, randomly generated SQL identifier.
        with engine.begin() as connection:
            connection.execute(text(f'REVOKE SELECT ON venue.memberships FROM "{role}"'))
        try:
            with pytest.raises(RuntimeError, match="^Token customization unavailable$"):
                invoke_token_trigger(token_event("admin-a"))
        finally:
            with engine.begin() as connection:
                connection.execute(text(f'GRANT SELECT ON venue.memberships TO "{role}"'))
        assert token_identity(invoke_token_trigger(token_event("admin-a")))[1]
    finally:
        engine.dispose()


def test_onboarding_idempotency_atomicity_and_private_operational_tables(env):
    body = {"name": "Venue", "slug": "venue", "initial_admin_sub": "admin"}
    request_headers = headers(key="create-venue-1")
    first = env.client.post("/v1/admin/venues", headers=request_headers, json=body)
    second = env.client.post("/v1/admin/venues", headers=request_headers, json=body)
    assert first.status_code == second.status_code == 201 and first.json() == second.json()
    assert first.headers["etag"] == '"1"'
    changed = env.client.post(
        "/v1/admin/venues", headers=request_headers, json={**body, "name": "Other"}
    )
    assert changed.status_code == 409
    duplicate = env.client.post(
        "/v1/admin/venues", headers=headers(), json={**body, "slug": "other"}
    )
    assert duplicate.status_code == 409
    with env.db["platform"].context(subject="operator") as session:
        assert session.scalar(text("SELECT count(*) FROM venue.venues")) == 1
        assert session.scalar(text("SELECT count(*) FROM venue.identity_sync_jobs")) == 1
    with env.db["worker"].context() as session:
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        event = DomainEvent.model_validate(session.scalar(select(OutboxEvent)).envelope)
        assert event.event_type == "venue.membership_changed"
    for statement in [
        "SELECT * FROM venue.audit_entries",
        "UPDATE venue.outbox_events SET state='sent'",
    ]:
        with pytest.raises(DBAPIError), env.db["api"].context(subject="admin") as session:
            session.execute(text(statement))

    listing = env.client.get("/v1/admin/venues", headers=headers()).json()
    assert len(listing["items"]) == 1 and not listing["has_more"]
    detail = env.client.get(f"/v1/admin/venues/{first.json()['venue']['id']}", headers=headers())
    assert detail.status_code == 200 and detail.headers["etag"] == '"1"'


def test_two_venues_are_isolated_at_http_and_rls_with_pooled_connections(env):
    a, b = onboard(env, "a"), onboard(env, "b")
    staff = add_member(env)
    rows = env.client.get("/v1/venues/current/members", headers=headers("admin-b")).json()["items"]
    assert [row["user_sub"] for row in rows] == ["admin-b"]
    response = env.client.patch(
        f"/v1/venues/current/members/{staff['id']}",
        headers=headers("admin-b", version=1),
        json={"role": "venue_admin"},
    )
    assert response.status_code == 404
    for data, expected in [(a, 2), (b, 1), (a, 2)]:
        with env.db["api"].context(subject="admin", tenant_id=UUID(data["venue"]["id"])) as session:
            assert len(session.scalars(select(Membership)).all()) == expected
    with env.db["api"].context() as session:
        assert session.scalars(select(Membership)).all() == []
    with env.db["identity"].context(subject="staff-a") as session:
        assert [member.user_sub for member in session.scalars(select(Membership))] == ["staff-a"]
    for mode, statement in [
        ("api", "UPDATE venue.venues SET status='suspended'"),
        ("worker", "UPDATE venue.memberships SET role='venue_admin'"),
        ("identity", "UPDATE venue.memberships SET role='venue_admin'"),
    ]:
        with pytest.raises(DBAPIError), env.db[mode].context() as session:
            session.execute(text(statement))


def test_staff_cannot_manage_roles_and_last_admin_cannot_be_removed(env):
    data = onboard(env)
    add_member(env)
    assert (
        env.client.get("/v1/venues/current/members", headers=headers("staff-a")).status_code == 403
    )
    response = env.client.patch(
        f"/v1/venues/current/members/{data['initial_admin']['id']}",
        headers=headers("admin-a", version=1),
        json={"status": "suspended"},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "last_venue_admin"
    response = env.client.post(
        "/v1/venues/current/members",
        headers=headers("admin-a"),
        json={"user_sub": "other", "role": "platform_admin"},
    )
    assert response.status_code == 422


def test_optimistic_versions_replay_and_no_caller_selected_tenant(env):
    onboard(env)
    staff = add_member(env)
    request_headers = headers("admin-a", key="change-staff-role", version=1)
    path = f"/v1/venues/current/members/{staff['id']}"
    response = env.client.patch(path, headers=request_headers, json={"role": "venue_admin"})
    assert response.status_code == 200 and response.headers["etag"] == '"2"'
    replay = env.client.patch(path, headers=request_headers, json={"role": "venue_admin"})
    assert replay.status_code == 200 and replay.json() == response.json()
    assert (
        env.client.patch(
            path, headers=headers("admin-a", version=1), json={"status": "suspended"}
        ).status_code
        == 412
    )
    assert (
        env.client.patch(path, headers=headers("admin-a"), json={"status": "suspended"}).status_code
        == 428
    )
    assert (
        env.client.post(
            "/v1/venues/current/members",
            headers=headers("admin-a"),
            json={"user_sub": "other", "role": "venue_staff", "tenant_id": str(uuid4())},
        ).status_code
        == 422
    )
    # An old staff token cannot use a role promotion until it is refreshed.
    assert (
        env.client.get("/v1/venues/current/members", headers=headers("staff-a")).status_code == 403
    )


def test_revoked_members_and_suspended_venues_reject_old_jwt_and_resolve_owner(env):
    data = onboard(env)
    staff = add_member(env)
    response = env.client.patch(
        f"/v1/venues/current/members/{staff['id']}",
        headers=headers("admin-a", version=1),
        json={"status": "suspended"},
    )
    assert response.status_code == 200
    assert env.client.get("/v1/venues/current", headers=headers("staff-a")).status_code == 403
    body = {
        "subject": "staff-a",
        "tenant_id": data["venue"]["id"],
        "allowed_roles": ["venue_staff"],
    }
    assert (
        env.client.post("/internal/v1/members/check", headers=headers("item"), json=body).json()[
            "active"
        ]
        is False
    )
    response = env.client.post(
        "/internal/v1/identity/resolve", headers=headers("identity"), json={"subject": "owner"}
    )
    assert response.json()["tenant_id"] is None
    assert (
        env.client.patch(
            f"/v1/admin/venues/{data['venue']['id']}",
            headers=headers(version=1),
            json={"status": "suspended"},
        ).status_code
        == 200
    )
    assert (
        env.client.post(
            "/v1/venues/current/members",
            headers=headers("admin-a"),
            json={"user_sub": "new", "role": "venue_staff"},
        ).status_code
        == 403
    )
    with env.db["identity"].context(subject="admin-a") as session:
        assert resolve_identity(session, "admin-a")["tenant_id"] is None


def test_one_active_venue_per_user_and_last_admin_concurrency(env):
    data = onboard(env)
    second = add_member(env, subject="second-admin", role="venue_admin")
    onboard(env, "b")
    assert (
        env.client.post(
            "/v1/venues/current/members",
            headers=headers("admin-b"),
            json={"user_sub": "second-admin", "role": "venue_staff"},
        ).status_code
        == 409
    )

    def demote(member_id):
        return env.client.patch(
            f"/v1/venues/current/members/{member_id}",
            headers=headers("admin-a", version=1),
            json={"role": "venue_staff"},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(demote, [data["initial_admin"]["id"], second["id"]]))
    assert statuses.count(200) == 1
    assert all(status in {200, 403, 409} for status in statuses)
    with env.db["api"].context(subject="admin", tenant_id=UUID(data["venue"]["id"])) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.role == "venue_admin", Membership.status == "active")
            )
            == 1
        )


def test_member_check_client_can_use_real_contract_and_fails_when_dependency_is_down(env):
    onboard(env)
    staff = add_member(env)
    import json

    from fastapi import HTTPException

    def transport(_url, _headers, body):
        response = env.client.post(
            "/internal/v1/members/check", headers=headers("item"), json=json.loads(body)
        )
        assert response.status_code == 200
        return response.json()

    client = MembershipClient("https://venue.invalid", lambda: "machine", transport=transport)
    assert client.require_current(env.identities["staff-a"], "venue_staff").active
    env.client.patch(
        f"/v1/venues/current/members/{staff['id']}",
        headers=headers("admin-a", version=1),
        json={"status": "suspended"},
    )
    with pytest.raises(HTTPException) as exc:
        client.require_current(env.identities["staff-a"], "venue_staff")
    assert exc.value.status_code == 403
    from lostlink_common.membership import MembershipUnavailable

    def unavailable(*_):
        raise MembershipUnavailable()

    with pytest.raises(HTTPException) as exc:
        MembershipClient(
            "https://venue.invalid", lambda: "token", transport=unavailable
        ).require_current(env.identities["staff-a"], "venue_staff")
    assert exc.value.status_code == 503


def test_identity_jobs_skip_obsolete_versions_and_retry_external_failures(env):
    data = onboard(env)
    staff = add_member(env)
    env.client.patch(
        f"/v1/venues/current/members/{staff['id']}",
        headers=headers("admin-a", version=1),
        json={"status": "suspended"},
    )

    class Revoker:
        def __init__(self):
            self.calls = []

        def revoke(self, subject):
            self.calls.append(subject)

    revoker = Revoker()
    runner = JobRunner(env.db["worker"], revoker=revoker)
    while runner.once("identity"):
        pass
    assert revoker.calls == ["admin-a", "staff-a"]
    with env.db["worker"].context() as session:
        jobs = list(session.scalars(select(IdentityJob)))
        assert sorted(job.state for job in jobs) == ["completed", "completed", "superseded"]
    response = env.client.get(
        f"/v1/admin/identity-sync/{data['initial_admin']['identity_sync_operation_id']}",
        headers=headers("admin-a"),
    )
    assert response.status_code == 200 and response.json()["state"] == "completed"


@pytest.mark.parametrize("clock_skew_seconds", [-3600, 0, 3600])
@pytest.mark.parametrize("model,mode", [(OutboxEvent, "outbox"), (IdentityJob, "identity")])
def test_worker_scheduling_uses_database_clock(env, monkeypatch, clock_skew_seconds, model, mode):
    onboard(env)

    class HostClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=clock_skew_seconds)

    # Inject the host-clock name to catch accidental reintroduction of datetime.now.
    monkeypatch.setattr(venue_workers, "datetime", HostClock, raising=False)
    runner = JobRunner(env.db["worker"])
    with env.db["worker"].context() as session:
        before_claim = session.scalar(select(func.now()))
    first = runner.claim(model)
    assert first is not None
    with env.db["worker"].context() as session:
        after_claim = session.scalar(select(func.now()))
        assert before_claim + timedelta(seconds=60) <= first.lease_until
        assert first.lease_until <= after_claim + timedelta(seconds=60)
    runner.finish(model, first, "pending", "dependency_unavailable")
    with env.db["worker"].context() as session:
        row = session.get(model, first.id)
        now = session.scalar(select(func.now()))
        assert after_claim + timedelta(seconds=1) <= row.available_at
        assert row.available_at <= now + timedelta(seconds=2)
        row.state = "failed"
    runner.retry_failed(mode, first.id, "operator", "dependency_restored")
    replay = runner.claim(model)
    assert replay is not None and replay.id == first.id


def test_outbox_leases_recovery_and_cas_prevent_stale_acknowledgement(env):
    onboard(env)
    runner = JobRunner(env.db["worker"])
    first = runner.claim(OutboxEvent)
    assert first is not None and runner.claim(OutboxEvent) is None
    with env.db["worker"].context() as session:
        row = session.get(OutboxEvent, first.id)
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    second = runner.claim(OutboxEvent)
    assert second.id == first.id and second.lease_token != first.lease_token
    runner.finish(OutboxEvent, first, "sent")
    with env.db["worker"].context() as session:
        assert session.get(OutboxEvent, first.id).state == "leased"
    runner.finish(OutboxEvent, second, "pending", "dependency_unavailable")
    with env.db["worker"].context() as session:
        session.get(OutboxEvent, first.id).available_at = datetime.now(UTC) - timedelta(seconds=1)

    class Publisher:
        def publish(self, event):
            assert str(event.event_id) == str(first.id)
            self.event = event

    publisher = Publisher()
    assert JobRunner(env.db["worker"], publisher=publisher).once("outbox")
    with env.db["worker"].context() as session:
        assert session.get(OutboxEvent, first.id).state == "sent"


def test_external_failure_exhaustion_and_audited_replay_preserve_event_identity(env):
    onboard(env)

    class BrokenPublisher:
        def publish(self, _):
            raise OSError("SNS unavailable")

    runner = JobRunner(env.db["worker"], publisher=BrokenPublisher())
    with env.db["worker"].context() as session:
        row = session.scalar(select(OutboxEvent))
        event_id, envelope = row.id, row.envelope
        row.attempts = 9
    assert runner.once("outbox")
    with env.db["worker"].context() as session:
        row = session.get(OutboxEvent, event_id)
        assert row.state == "failed" and row.attempts == 10
    runner.retry_failed("outbox", event_id, "operator", "dependency_restored")
    with env.db["worker"].context() as session:
        row = session.get(OutboxEvent, event_id)
        assert row.state == "pending" and row.attempts == 0 and row.envelope == envelope
    engine = create_engine(env.migration_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM venue.audit_entries "
                        "WHERE action='outbox.retry.dependency_restored' AND resource_id=:id"
                    ),
                    {"id": event_id},
                )
                == 1
            )
    finally:
        engine.dispose()


def test_identity_dependency_failure_is_durable_and_can_recover(env):
    onboard(env)

    class BrokenRevoker:
        def revoke(self, _):
            raise OSError("Cognito unavailable")

    assert JobRunner(env.db["worker"], revoker=BrokenRevoker()).once("identity")
    with env.db["worker"].context() as session:
        job = session.scalar(select(IdentityJob))
        assert job.state == "pending" and job.attempts == 1
        assert job.last_error_code == "dependency_unavailable"
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)

    class Revoker:
        def revoke(self, _):
            pass

    assert JobRunner(env.db["worker"], revoker=Revoker()).once("identity")
    with env.db["worker"].context() as session:
        assert session.scalar(select(IdentityJob)).state == "completed"


def test_committed_membership_event_reaches_sns_and_sqs(env):
    import json

    endpoint = os.getenv("LOSTLINK_TEST_AWS_ENDPOINT")
    if not endpoint:
        pytest.skip("Set LOSTLINK_TEST_AWS_ENDPOINT to a LocalStack URL")
    settings = Settings(_env_file=None, environment="test", aws_endpoint_url=endpoint)
    sns, sqs = aws_client("sns", settings), aws_client("sqs", settings)
    name = f"venue-test-{uuid4().hex}"
    topic, queue = None, None
    try:
        topic = sns.create_topic(Name=name)["TopicArn"]
        queue = sqs.create_queue(QueueName=name)["QueueUrl"]
        arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        sqs.set_queue_attributes(
            QueueUrl=queue,
            Attributes={
                "Policy": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "sns.amazonaws.com"},
                                "Action": "sqs:SendMessage",
                                "Resource": arn,
                                "Condition": {"ArnEquals": {"aws:SourceArn": topic}},
                            }
                        ],
                    }
                )
            },
        )
        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=arn)
        data = onboard(env)
        assert JobRunner(env.db["worker"], publisher=SnsPublisher(sns, topic)).once("outbox")
        messages = sqs.receive_message(QueueUrl=queue, WaitTimeSeconds=5).get("Messages", [])
        assert len(messages) == 1
        event = decode_message(messages[0]["Body"])
        assert event.event_type == "venue.membership_changed"
        assert str(event.tenant_id) == data["venue"]["id"]
        assert event.data["membership_id"] == data["initial_admin"]["id"]
    finally:
        if topic:
            sns.delete_topic(TopicArn=topic)
        if queue:
            sqs.delete_queue(QueueUrl=queue)
        sns.close()
        sqs.close()


def test_readiness_role_safety_and_migrations_are_schema_scoped(env):
    assert env.client.get("/ready").status_code == 200
    for database in env.db.values():
        database.check_ready("venue", DATABASE_REVISION)
    unsafe = VenueDatabase(Settings(_env_file=None, database_url=env.migration_url))
    try:
        with pytest.raises(RuntimeError), unsafe.context() as session:
            session.execute(text("SELECT 1"))
    finally:
        unsafe.close()
    engine = create_engine(env.migration_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA other_service"))
            connection.execute(text("CREATE TABLE other_service.items (id integer PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE public.unrelated (id integer PRIMARY KEY)"))
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("MIGRATION_DATABASE_URL", env.migration_url)
            for mode, role in env.roles.items():
                patch.setenv(f"VENUE_{mode.upper()}_ROLE", role)
            command.check(env.cfg)
            command.downgrade(env.cfg, "base")
            command.upgrade(env.cfg, "head")
        env.db["api"].check_ready("venue", DATABASE_REVISION)
    finally:
        engine.dispose()
