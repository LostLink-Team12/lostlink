"""Schema-scoped migrations and explicit least-privilege role grants."""

from alembic import context
from lostlink_common.db import identifier
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, pool, text
from venue_app.models import DATABASE_SCHEMA, Base


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    migration_database_url: SecretStr
    venue_api_role: str = "lostlink_venue_api"
    venue_platform_role: str = "lostlink_venue_platform"
    venue_worker_role: str = "lostlink_venue_worker"
    venue_identity_role: str = "lostlink_venue_identity"


settings = MigrationSettings()
roles = {
    "api": settings.venue_api_role,
    "platform": settings.venue_platform_role,
    "worker": settings.venue_worker_role,
    "identity": settings.venue_identity_role,
}
if len(set(roles.values())) != 4:
    raise ValueError("Venue database roles must be distinct")
quoted = {mode: identifier(role) for mode, role in roles.items()}
if context.is_offline_mode():
    raise RuntimeError("Use online migrations to validate role grants")


def include_name(name, type_, _parent_names):
    return name == DATABASE_SCHEMA if type_ == "schema" else True


def grants(connection):
    tenant = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    subject = "user_sub = NULLIF(current_setting('app.user_sub', true), '')"
    actor = "actor_sub = NULLIF(current_setting('app.user_sub', true), '')"
    policies = {
        "venues": {
            "api": tenant.replace("tenant_id =", "id ="),
            "platform": "true",
            "worker": tenant.replace("tenant_id =", "id ="),
            "identity": "true",
        },
        "memberships": {"api": tenant, "platform": "true", "worker": tenant, "identity": subject},
        "identity_sync_jobs": {"api": tenant, "platform": "true", "worker": "true"},
        "outbox_events": {"api": tenant, "platform": "true", "worker": "true"},
        "idempotency_records": {"api": actor, "platform": actor},
        "audit_entries": {"api": f"({tenant}) AND ({actor})", "platform": actor, "worker": actor},
    }
    exists = connection.scalar(text("SELECT to_regclass('venue.venues')"))
    for mode, role in quoted.items():
        flags = connection.execute(
            text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=:name"),
            {"name": roles[mode]},
        ).one()
        if flags.rolsuper or flags.rolbypassrls:
            raise ValueError("Runtime roles cannot bypass RLS")
        if connection.scalar(text("SELECT current_user")) == roles[mode]:
            raise ValueError("Runtime role cannot own migrations")
        connection.execute(text(f"GRANT USAGE ON SCHEMA venue TO {role}"))
        connection.execute(text(f"GRANT SELECT ON venue.alembic_version TO {role}"))
    if not exists:
        return
    for table, modes in policies.items():
        qualified = f"venue.{identifier(table)}"
        connection.execute(text(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"))
        connection.execute(text(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY"))
        connection.execute(text(f"REVOKE ALL ON {qualified} FROM PUBLIC"))
        for mode, predicate in modes.items():
            policy = identifier(f"{table}_{mode}")
            connection.execute(text(f"DROP POLICY IF EXISTS {policy} ON {qualified}"))
            connection.execute(
                text(
                    f"CREATE POLICY {policy} ON {qualified} TO {quoted[mode]} "
                    f"USING ({predicate}) WITH CHECK ({predicate})"
                )
            )
    permissions = {
        "api": {
            "venues": "SELECT",
            "memberships": "SELECT,INSERT,UPDATE",
            "identity_sync_jobs": "SELECT,INSERT",
            "outbox_events": "INSERT",
            "audit_entries": "INSERT",
            "idempotency_records": "SELECT,INSERT,DELETE",
        },
        "platform": {
            "venues": "SELECT,INSERT,UPDATE",
            "memberships": "SELECT,INSERT",
            "identity_sync_jobs": "SELECT,INSERT",
            "outbox_events": "INSERT",
            "audit_entries": "INSERT",
            "idempotency_records": "SELECT,INSERT,DELETE",
        },
        "worker": {
            "venues": "SELECT",
            "memberships": "SELECT",
            "identity_sync_jobs": "SELECT,UPDATE",
            "outbox_events": "SELECT,UPDATE",
            "audit_entries": "INSERT",
        },
        "identity": {"venues": "SELECT", "memberships": "SELECT"},
    }
    for mode, tables in permissions.items():
        for table, permission in tables.items():
            connection.execute(
                text(f"GRANT {permission} ON venue.{identifier(table)} TO {quoted[mode]}")
            )
    # PostgreSQL SELECT FOR UPDATE requires an UPDATE privilege on at least one column.
    # The tenant role may lock the venue but cannot update its status/name/slug.
    connection.execute(text(f"GRANT UPDATE(version) ON venue.venues TO {quoted['api']}"))
    for mode in ["api", "platform", "worker"]:
        for table in ["audit_entries", "outbox_events"]:
            connection.execute(
                text(f"GRANT SELECT(id,created_at) ON venue.{table} TO {quoted[mode]}")
            )
        connection.execute(
            text(f"GRANT SELECT(available_at) ON venue.outbox_events TO {quoted[mode]}")
        )


engine = create_engine(settings.migration_database_url.get_secret_value(), poolclass=pool.NullPool)
try:
    with engine.connect() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS venue"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table_schema="venue",
            include_schemas=True,
            include_name=include_name,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
            grants(connection)
finally:
    engine.dispose()
