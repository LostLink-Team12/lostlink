"""Run the real deployment tools against disposable PostgreSQL resources."""

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from scripts import backend_admin as tool

pytestmark = pytest.mark.integration


@pytest.fixture
def backend(monkeypatch, tmp_path):
    admin_url = os.getenv("LOSTLINK_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set LOSTLINK_TEST_ADMIN_URL to a disposable local PostgreSQL administrator")
    base = make_url(admin_url)
    suffix = uuid4().hex
    name = f"ll_bootstrap_{suffix}"
    document = json.loads((tool.ROOT / "infra/database/backend.example.json").read_text())
    document["database"] = name
    url = base.set(database=name)
    monkeypatch.setenv("BOOTSTRAP_DATABASE_URL", url.render_as_string(hide_password=False))
    for service, settings in document["services"].items():
        monkeypatch.setenv(settings["migration_url_env"], url.render_as_string(hide_password=False))
        for mode, role in settings["roles"].items():
            role["name"] = f"ll_b_{service}_{mode}_{suffix}"
            # Exercise escaping and URL encoding, not only alphanumeric passwords.
            monkeypatch.setenv(role["password_env"], f"{uuid4().hex} '%\\@:")
    path = tmp_path / "backend.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    config = tool.load(path)
    additional_roles = []
    dsn = base.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield SimpleNamespace(
                config=config,
                additional_roles=additional_roles,
                path=path,
                url=url,
                run=lambda action, *extra: tool.main(
                    [
                        action,
                        "--config",
                        str(path),
                        "--environment",
                        "local",
                        *extra,
                    ]
                ),
            )
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            for service in config.services.values():
                for role in service.roles.values():
                    admin.execute(
                        sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role.name))
                    )
            for role in additional_roles:
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_all_five_services_bootstrap_migrate_and_check_are_repeatable(backend, capsys):
    assert backend.run("bootstrap") == 0
    assert json.loads(capsys.readouterr().out)["created_roles"] == 15
    assert backend.run("bootstrap") == 0
    assert json.loads(capsys.readouterr().out)["created_roles"] == 0
    assert backend.run("migrate") == 0
    assert backend.run("check") == 0
    assert backend.run("migrate") == 0
    assert backend.run("check") == 0
    # Read-only checking must not silently repair an incorrect revision.
    with tool.connect(backend.url) as connection:
        connection.execute("UPDATE venue.alembic_version SET version_num='stale'")
    assert backend.run("check", "--service", "venue") == 1
    output = capsys.readouterr().out
    assert "migration_revision_mismatch:venue" in output
    assert backend.run("migrate", "--service", "venue") == 1
    assert "migration_failed:venue" in capsys.readouterr().out
    with tool.connect(backend.url) as connection:
        assert connection.execute("SELECT version_num FROM venue.alembic_version").fetchone() == (
            "stale",
        )


def test_existing_password_mismatch_does_not_rotate_it_or_create_other_roles(
    backend, monkeypatch, capsys
):
    assert backend.run("bootstrap", "--service", "venue") == 0
    role = backend.config.services["venue"].roles["api"]
    password = os.environ[role.password_env]
    monkeypatch.setenv(role.password_env, "a-different-secret-value")
    assert backend.run("bootstrap") == 1
    assert backend.run("migrate") == 1
    output = capsys.readouterr().out
    assert password not in output and "a-different-secret-value" not in output
    with tool.connect(backend.url) as connection:
        name = backend.config.services["item"].roles["api"].name
        assert not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone()
        assert not connection.execute("SELECT 1 FROM pg_namespace WHERE nspname='venue'").fetchone()
    tool.authenticate(backend.url, role, password)


@pytest.mark.parametrize("unsafe", ["superuser", "membership", "ownership"])
def test_bootstrap_refuses_existing_unsafe_roles_before_other_changes(backend, unsafe, capsys):
    assert backend.run("bootstrap", "--service", "venue") == 0
    role = backend.config.services["venue"].roles["api"].name
    other = backend.config.services["venue"].roles["worker"].name
    with tool.connect(backend.url) as connection:
        if unsafe == "superuser":
            connection.execute(sql.SQL("ALTER ROLE {} SUPERUSER").format(sql.Identifier(role)))
        elif unsafe == "membership":
            connection.execute(
                sql.SQL("GRANT {} TO {}").format(sql.Identifier(other), sql.Identifier(role))
            )
        else:
            connection.execute(sql.SQL("CREATE TABLE public.owned (id integer)"))
            connection.execute(
                sql.SQL("ALTER TABLE public.owned OWNER TO {}").format(sql.Identifier(role))
            )
    assert backend.run("bootstrap") == 1
    expected = {
        "superuser": "role_attributes_unsafe",
        "membership": "role_membership_unsafe",
        "ownership": "runtime_role_owns_objects",
    }
    assert expected[unsafe] in capsys.readouterr().out
    with tool.connect(backend.url) as connection:
        name = backend.config.services["item"].roles["api"].name
        assert not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone()


def test_checks_detect_rls_and_cross_service_and_private_column_grant_drift(backend, capsys):
    assert backend.run("bootstrap") == 0
    assert backend.run("migrate") == 0
    assert backend.run("check") == 0
    with tool.connect(backend.url) as connection:
        connection.execute("ALTER TABLE venue.memberships DISABLE ROW LEVEL SECURITY")
    assert backend.run("check", "--service", "venue") == 1
    assert "table_or_forced_rls_invalid" in capsys.readouterr().out
    with tool.connect(backend.url) as connection:
        connection.execute("ALTER TABLE venue.memberships ENABLE ROW LEVEL SECURITY")
        connection.execute("GRANT SELECT ON venue.memberships TO PUBLIC")
    assert backend.run("check", "--service", "venue") == 1
    assert "public_data_privileges" in capsys.readouterr().out
    with tool.connect(backend.url) as connection:
        connection.execute("REVOKE SELECT ON venue.memberships FROM PUBLIC")
        role = backend.config.services["venue"].roles["api"].name
        connection.execute(
            sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(sql.Identifier(role))
        )
    assert backend.run("check", "--service", "venue") == 1
    assert "runtime_can_create_objects" in capsys.readouterr().out
    with tool.connect(backend.url) as connection:
        connection.execute(
            sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(sql.Identifier(role))
        )
        role = backend.config.services["venue"].roles["identity"].name
        connection.execute(sql.SQL("GRANT SELECT ON item.items TO {}").format(sql.Identifier(role)))
    assert backend.run("check", "--service", "venue") == 1
    assert "runtime_excess_privileges" in capsys.readouterr().out
    with tool.connect(backend.url) as connection:
        connection.execute(
            sql.SQL("REVOKE SELECT ON item.items FROM {}").format(sql.Identifier(role))
        )
        role = backend.config.services["item"].roles["internal"].name
        connection.execute(
            sql.SQL("GRANT SELECT(reference_answer) ON item.verification_questions TO {}").format(
                sql.Identifier(role)
            )
        )
    assert backend.run("check", "--service", "item") == 1
    assert "private_column_access" in capsys.readouterr().out


def test_a_separate_non_superuser_can_own_and_repeat_venue_migrations(backend, monkeypatch):
    assert backend.run("bootstrap", "--service", "venue") == 0
    name, password = f"ll_m_{uuid4().hex}", uuid4().hex
    backend.additional_roles.append(name)
    with tool.connect(backend.url) as connection:
        connection.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOINHERIT NOREPLICATION NOBYPASSRLS"
            ).format(sql.Identifier(name), sql.Literal(password))
        )
        connection.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                sql.Identifier(backend.config.database),
                sql.Identifier(name),
            )
        )
    migration_url = backend.url.set(username=name, password=password)
    monkeypatch.setenv(
        "VENUE_MIGRATION_DATABASE_URL", migration_url.render_as_string(hide_password=False)
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-not-for-migration")
    monkeypatch.setenv("FIELD_ENCRYPTION_KEYS", "test-not-for-migration")
    original = tool.subprocess.run

    def isolated_child(*args, **kwargs):
        child = kwargs["env"]
        assert child["MIGRATION_DATABASE_URL"] == migration_url.render_as_string(
            hide_password=False
        )
        assert "BOOTSTRAP_DATABASE_URL" not in child
        assert "AWS_ACCESS_KEY_ID" not in child and "FIELD_ENCRYPTION_KEYS" not in child
        for settings in backend.config.services.values():
            assert all(role.password_env not in child for role in settings.roles.values())
        return original(*args, **kwargs)

    monkeypatch.setattr(tool.subprocess, "run", isolated_child)
    assert backend.run("migrate", "--service", "venue") == 0
    assert backend.run("migrate", "--service", "venue") == 0
    assert backend.run("check", "--service", "venue") == 0


def test_actual_runtime_login_cannot_be_used_as_a_migration_identity(backend, monkeypatch, capsys):
    assert backend.run("bootstrap", "--service", "venue") == 0
    role = backend.config.services["venue"].roles["api"]
    url = backend.url.set(username=role.name, password=os.environ[role.password_env])
    monkeypatch.setenv("VENUE_MIGRATION_DATABASE_URL", url.render_as_string(hide_password=False))
    assert backend.run("migrate", "--service", "venue") == 1
    assert "migration_identity_is_runtime_role" in capsys.readouterr().out
