"""Real PostgreSQL, independent login roles and disposable test-owned resources."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
