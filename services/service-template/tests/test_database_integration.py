"""Use a newly created database and role; never migrate or clear an existing database."""

import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from app.main import create_app
from app.models import DATABASE_REVISION, DATABASE_SCHEMA, ExampleRecord
from fastapi.testclient import TestClient
from lostlink_common.auth import InvalidToken, Principal
from lostlink_common.config import Settings
from lostlink_common.db import Database
from psycopg import sql
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database_fixture():
    admin_url = os.getenv("LOSTLINK_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set LOSTLINK_TEST_ADMIN_URL to a local PostgreSQL admin URL")
    base_url = make_url(admin_url)
    suffix = uuid4().hex
    db_name = f"ll_test_{suffix}"
    role = f"ll_app_{suffix}"
    password = uuid4().hex
    admin_dsn = base_url.set(drivername="postgresql").render_as_string(hide_password=False)
    role_created = False
    database_created = False
    database = None
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        try:
            admin.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
            role_created = True
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
            database_created = True
            migration_url = base_url.set(database=db_name).render_as_string(hide_password=False)
            runtime_url = base_url.set(
                database=db_name, username=role, password=password
            ).render_as_string(hide_password=False)
            cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("MIGRATION_DATABASE_URL", migration_url)
                patch.setenv("DATABASE_APP_ROLE", role)
                command.upgrade(cfg, "head")
                command.upgrade(cfg, "head")
            database = Database(
                Settings(
                    _env_file=None, database_url=runtime_url, db_pool_size=1, db_max_overflow=0
                )
            )
            yield database, migration_url, runtime_url, role, cfg
        finally:
            if database is not None:
                database.close()
            if database_created:
                # Both names are generated above; no caller-supplied database is dropped.
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(db_name)))
            if role_created:
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_migration_and_readiness(database_fixture):
    database, *_ = database_fixture
    database.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
    with database.engine.connect() as connection:
        flags = connection.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = 'service_template.example_records'::regclass"
            )
        ).one()
        assert flags.relrowsecurity and flags.relforcerowsecurity
        assert not connection.execute(
            text("SELECT has_schema_privilege(current_user, 'service_template', 'CREATE')")
        ).scalar_one()
        assert not connection.execute(
            text(
                "SELECT has_table_privilege("
                "current_user, 'service_template.alembic_version', 'UPDATE')"
            )
        ).scalar_one()
    with pytest.raises(RuntimeError, match="migration"):
        database.check_ready(DATABASE_SCHEMA, "not-current")


def test_rls_isolates_reads_updates_and_deletes_on_reused_connection(database_fixture):
    database, *_ = database_fixture
    first, second = uuid4(), uuid4()
    with database.session(first) as session:
        row = ExampleRecord(tenant_id=first, name="private-first")
        session.add(row)
        session.flush()
        record_id = row.id
        backend_pid = session.execute(text("SELECT pg_backend_pid()")).scalar_one()
    with database.session(second) as session:
        assert session.execute(text("SELECT pg_backend_pid()")).scalar_one() == backend_pid
        assert session.scalars(select(ExampleRecord)).all() == []
        assert (
            session.execute(
                text("UPDATE service_template.example_records SET name='changed' WHERE id=:id"),
                {"id": record_id},
            ).rowcount
            == 0
        )
        assert (
            session.execute(
                text("DELETE FROM service_template.example_records WHERE id=:id"), {"id": record_id}
            ).rowcount
            == 0
        )
        session.add(ExampleRecord(tenant_id=second, name="private-second"))
    with database.session(first) as session:
        assert [row.name for row in session.scalars(select(ExampleRecord))] == ["private-first"]


def test_rls_rejects_cross_tenant_insert_and_reassignment(database_fixture):
    database, *_ = database_fixture
    first, second = uuid4(), uuid4()
    with pytest.raises(DBAPIError, match="row-level security"):
        with database.session(first) as session:
            session.add(ExampleRecord(tenant_id=second, name="forged"))
    with database.session(first) as session:
        record = ExampleRecord(tenant_id=first, name="original")
        session.add(record)
    with pytest.raises(DBAPIError, match="row-level security"):
        with database.session(first) as session:
            session.execute(
                text("UPDATE service_template.example_records SET tenant_id=:tenant WHERE id=:id"),
                {"tenant": second, "id": record.id},
            )


def test_absent_context_fails_closed_and_transaction_context_does_not_leak(database_fixture):
    database, *_ = database_fixture
    tenant = uuid4()
    with database.session(tenant) as session:
        session.add(ExampleRecord(tenant_id=tenant, name="hidden"))
    with database.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM service_template.example_records")
            ).scalar_one()
            == 0
        )
        with pytest.raises(DBAPIError, match="row-level security"):
            connection.execute(
                text(
                    "INSERT INTO service_template.example_records (id, tenant_id, name) "
                    "VALUES (:id, :tenant, 'forged')"
                ),
                {"id": uuid4(), "tenant": tenant},
            )
    with pytest.raises(ValueError):
        with database.session(None):
            pass


def test_failed_transaction_rolls_back(database_fixture):
    database, *_ = database_fixture
    tenant = uuid4()
    with pytest.raises(RuntimeError, match="cancel"):
        with database.session(tenant) as session:
            session.add(ExampleRecord(tenant_id=tenant, name="must-rollback"))
            session.flush()
            raise RuntimeError("cancel")
    with database.session(tenant) as session:
        assert session.scalars(select(ExampleRecord)).all() == []


def test_superuser_runtime_is_rejected(database_fixture):
    _, migration_url, *_ = database_fixture
    unsafe = Database(Settings(_env_file=None, database_url=migration_url))
    try:
        with pytest.raises(RuntimeError, match="bypass"):
            with unsafe.session(uuid4()) as session:
                session.execute(text("SELECT 1"))
        with pytest.raises(RuntimeError, match="bypass"):
            unsafe.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
    finally:
        unsafe.close()


def test_protected_http_storage_and_validation(database_fixture):
    database, *_ = database_fixture
    first, second = uuid4(), uuid4()

    class Verifier:
        def verify(self, token):
            try:
                tenant = UUID(token)
            except ValueError:
                raise InvalidToken() from None
            return Principal(
                subject="staff-1", client_id="venue-client", tenant_id=tenant, roles={"venue_staff"}
            )

    app = create_app(Settings(_env_file=None), token_verifier=Verifier(), database=database)
    with TestClient(app) as client:
        first_headers = {"Authorization": f"Bearer {first}"}
        second_headers = {"Authorization": f"Bearer {second}"}
        assert client.get("/ready").status_code == 200
        response = client.post("/v1/examples", headers=first_headers, json={"name": "test"})
        assert response.status_code == 201
        record_id = response.json()["id"]
        assert client.get("/v1/examples", headers=first_headers).json()[0]["id"] == record_id
        assert client.get("/v1/examples", headers=second_headers).json() == []
        response = client.post(
            "/v1/examples", headers=first_headers, json={"name": "test", "tenant_id": str(second)}
        )
        assert response.status_code == 422
        assert str(second) not in response.text
        assert client.get("/v1/examples?limit=1000", headers=first_headers).status_code == 422
        assert client.get("/v1/examples", headers={"X-Tenant-ID": str(first)}).status_code == 401


def test_autogenerate_ignores_other_service_schemas(database_fixture):
    _, migration_url, _, role, cfg = database_fixture
    engine = create_engine(migration_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA other_service"))
            connection.execute(text("CREATE TABLE other_service.items (id integer PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE public.unrelated (id integer PRIMARY KEY)"))
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("MIGRATION_DATABASE_URL", migration_url)
            patch.setenv("DATABASE_APP_ROLE", role)
            command.check(cfg)
    finally:
        engine.dispose()


def test_migration_downgrade_and_upgrade(database_fixture):
    database, migration_url, _, role, cfg = database_fixture
    database.close()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MIGRATION_DATABASE_URL", migration_url)
        patch.setenv("DATABASE_APP_ROLE", role)
        command.downgrade(cfg, "base")
        engine = create_engine(migration_url)
        try:
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text("SELECT to_regclass('service_template.example_records')")
                    ).scalar()
                    is None
                )
        finally:
            engine.dispose()
        command.upgrade(cfg, "head")
    database.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
