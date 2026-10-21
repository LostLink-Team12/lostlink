"""Owner, venue and system contexts against real PostgreSQL RLS with a restricted role."""

import os
from uuid import uuid4

import psycopg
import pytest
from lostlink_common.auth import Principal
from lostlink_common.config import Settings
from lostlink_common.db import Database, enable_owner_or_tenant_rls
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration

SCHEMA = "shared_ctx"
TENANT_A, TENANT_B = uuid4(), uuid4()


@pytest.fixture(scope="module")
def database():
    admin_url = os.getenv("LOSTLINK_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set LOSTLINK_TEST_ADMIN_URL to a local PostgreSQL admin URL")
    base_url = make_url(admin_url)
    suffix = uuid4().hex
    db_name, role, password = f"ll_test_{suffix}", f"ll_app_{suffix}", uuid4().hex
    admin_dsn = base_url.set(drivername="postgresql").render_as_string(hide_password=False)
    role_created = database_created = False
    runtime = None
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
            engine = create_engine(migration_url)
            try:
                with engine.begin() as connection:
                    connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
                    connection.execute(
                        text(
                            f"CREATE TABLE {SCHEMA}.claims (id uuid PRIMARY KEY, "
                            "tenant_id uuid NOT NULL, owner_sub text NOT NULL, note text)"
                        )
                    )
                    enable_owner_or_tenant_rls(connection, SCHEMA, "claims")
                    connection.execute(
                        text(f"CREATE TABLE {SCHEMA}.jobs (id uuid PRIMARY KEY, state text)")
                    )
                    grants = [
                        f"GRANT USAGE ON SCHEMA {SCHEMA} TO {{role}}",
                        f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.claims TO {{role}}",
                        f"GRANT SELECT, INSERT ON {SCHEMA}.jobs TO {{role}}",
                    ]
                    for grant in grants:
                        connection.execute(text(grant.format(role=f'"{role}"')))
            finally:
                engine.dispose()
            runtime_url = base_url.set(
                database=db_name, username=role, password=password
            ).render_as_string(hide_password=False)
            runtime = Database(
                Settings(
                    _env_file=None, database_url=runtime_url, db_pool_size=1, db_max_overflow=0
                )
            )
            with runtime.scoped_session(tenant_id=TENANT_A) as session:
                insert_claim(session, TENANT_A, "owner-1", "a-owner-1")
                insert_claim(session, TENANT_A, "owner-2", "a-owner-2")
            with runtime.scoped_session(tenant_id=TENANT_B) as session:
                insert_claim(session, TENANT_B, "owner-1", "b-owner-1")
            yield runtime
        finally:
            if runtime is not None:
                runtime.close()
            if database_created:
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(db_name)))
            if role_created:
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def insert_claim(session, tenant, owner, note):
    session.execute(
        text(
            f"INSERT INTO {SCHEMA}.claims (id, tenant_id, owner_sub, note) "
            "VALUES (:id, :tenant, :owner, :note)"
        ),
        {"id": uuid4(), "tenant": tenant, "owner": owner, "note": note},
    )


def notes(session):
    return sorted(session.scalars(text(f"SELECT note FROM {SCHEMA}.claims")))


def owner(subject):
    return Principal(subject=subject, client_id="web", roles={"owner"})


def staff(subject, tenant):
    return Principal(subject=subject, client_id="web", tenant_id=tenant, roles={"venue_staff"})


def test_owner_sees_only_own_rows_across_venues(database):
    with database.actor_session(owner("owner-1"), venue_scope=False) as session:
        assert notes(session) == ["a-owner-1", "b-owner-1"]
        assert session.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar() == ""
        updated = session.execute(
            text(f"UPDATE {SCHEMA}.claims SET note = note WHERE owner_sub = 'owner-2'")
        ).rowcount
        assert updated == 0


def test_owner_cannot_write_rows_for_another_owner(database):
    with pytest.raises(DBAPIError, match="row-level security"):
        with database.actor_session(owner("owner-1"), venue_scope=False) as session:
            insert_claim(session, TENANT_A, "owner-2", "forged")
    with database.actor_session(owner("owner-3"), venue_scope=False) as session:
        insert_claim(session, TENANT_B, "owner-3", "b-owner-3")
        assert notes(session) == ["b-owner-3"]
    with database.actor_session(staff("staff-a", TENANT_A), venue_scope=True) as session:
        assert "b-owner-3" not in notes(session)


def test_venue_actor_sees_only_their_venue(database):
    with database.actor_session(staff("staff-a", TENANT_A), venue_scope=True) as session:
        assert notes(session) == ["a-owner-1", "a-owner-2"]
    with pytest.raises(DBAPIError, match="row-level security"):
        with database.actor_session(staff("staff-a", TENANT_A), venue_scope=True) as session:
            insert_claim(session, TENANT_B, "owner-9", "cross-venue")


def test_permissive_policies_combine_for_staff_who_are_also_owners(database):
    # Services must reject self-review explicitly; RLS intentionally shows both sets.
    with database.actor_session(staff("owner-1", TENANT_A), venue_scope=True) as session:
        assert notes(session) == ["a-owner-1", "a-owner-2", "b-owner-1"]


def test_system_session_has_no_actor_rows_but_keeps_role_grants(database):
    with database.system_session() as session:
        assert notes(session) == []
        session.execute(
            text(f"INSERT INTO {SCHEMA}.jobs (id, state) VALUES (:id, 'pending')"),
            {"id": uuid4()},
        )
        assert session.scalar(text(f"SELECT count(*) FROM {SCHEMA}.jobs")) >= 1
    with pytest.raises(DBAPIError, match="permission denied"):
        with database.system_session() as session:
            session.execute(text(f"DELETE FROM {SCHEMA}.jobs"))


def test_context_is_transaction_local_on_a_reused_connection(database):
    with database.actor_session(owner("owner-1"), venue_scope=False) as session:
        pid = session.scalar(text("SELECT pg_backend_pid()"))
        assert notes(session)
    with database.system_session() as session:
        assert session.scalar(text("SELECT pg_backend_pid()")) == pid
        assert session.scalar(text("SELECT current_setting('app.user_sub', true)")) == ""
        assert notes(session) == []
    with database.session(TENANT_B) as session:
        assert session.scalar(text("SELECT pg_backend_pid()")) == pid
        visible = notes(session)
        assert "b-owner-1" in visible and all(note.startswith("b-") for note in visible)
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT current_setting('app.user_sub', true)")) in {
            None,
            "",
        }
        assert connection.scalar(text(f"SELECT count(*) FROM {SCHEMA}.claims")) == 0
