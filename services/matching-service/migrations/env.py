"""Three non-owner roles; owner RLS never expands into a global private-report API."""

from alembic import context
from lostlink_common.db import identifier
from matching_app.models import Base
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, pool, text


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    migration_database_url: SecretStr
    matching_api_role: str = "lostlink_matching_api"
    matching_internal_role: str = "lostlink_matching_internal"
    matching_worker_role: str = "lostlink_matching_worker"


settings = MigrationSettings()
roles = {mode: getattr(settings, f"matching_{mode}_role") for mode in ["api", "internal", "worker"]}
if len(set(roles.values())) != 3:
    raise ValueError("Matching runtime roles must be distinct")
quoted = {mode: identifier(role) for mode, role in roles.items()}
if context.is_offline_mode():
    raise RuntimeError("Online migrations are required to validate role grants")


def include_name(name, type_, _parent_names):
    return name == "matching" if type_ == "schema" else True


def grants(connection):
    owner = "owner_id = NULLIF(current_setting('app.user_sub', true), '')"
    actor = "actor_sub = NULLIF(current_setting('app.user_sub', true), '')"
    resource = "id = NULLIF(current_setting('app.resource_id', true), '')::uuid"
    for mode, role in roles.items():
        flags = connection.execute(
            text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=:name"), {"name": role}
        ).one()
        if (
            flags.rolsuper
            or flags.rolbypassrls
            or connection.scalar(text("SELECT current_user")) == role
        ):
            raise ValueError("Runtime roles cannot own tables or bypass RLS")
        connection.execute(text(f"GRANT USAGE ON SCHEMA matching TO {quoted[mode]}"))
        connection.execute(text(f"GRANT SELECT ON matching.alembic_version TO {quoted[mode]}"))
    if not connection.scalar(text("SELECT to_regclass('matching.reports')")):
        return
    policies = {
        "catalog": {"api": "state = 'available'", "worker": "true"},
        "item_vectors": {
            "api": "EXISTS (SELECT 1 FROM matching.catalog c "
            "WHERE c.item_id = item_vectors.item_id "
            "AND c.state = 'available' "
            "AND c.item_version = item_vectors.item_version "
            "AND c.tenant_id = item_vectors.tenant_id)",
            "worker": "true",
        },
        "reports": {
            "api": owner,
            "worker": "true",
            "internal": "EXISTS (SELECT 1 FROM matching.matches m WHERE m.report_id = reports.id "
            "AND m.id = NULLIF(current_setting('app.resource_id', true), '')::uuid)",
        },
        "matches": {"api": owner, "worker": "true", "internal": resource},
        "work_jobs": {"api": owner, "worker": "true"},
        "outbox_events": {"api": owner, "worker": "true"},
        "inbox_receipts": {"worker": "true"},
        "idempotency_records": {"api": actor},
        "audit_entries": {"api": actor, "worker": actor},
    }
    for table, modes in policies.items():
        connection.execute(text(f"ALTER TABLE matching.{table} ENABLE ROW LEVEL SECURITY"))
        connection.execute(text(f"ALTER TABLE matching.{table} FORCE ROW LEVEL SECURITY"))
        connection.execute(text(f"REVOKE ALL ON matching.{table} FROM PUBLIC"))
        for mode, predicate in modes.items():
            name = identifier(f"{table}_{mode}")
            connection.execute(text(f"DROP POLICY IF EXISTS {name} ON matching.{table}"))
            connection.execute(
                text(
                    f"CREATE POLICY {name} ON matching.{table} TO {quoted[mode]} "
                    f"USING ({predicate}) WITH CHECK ({predicate})"
                )
            )
    permissions = {
        "api": {
            "catalog": "SELECT",
            "item_vectors": "SELECT",
            "reports": "SELECT,INSERT,UPDATE",
            "matches": "SELECT",
            "idempotency_records": "SELECT,INSERT,DELETE",
            "work_jobs": "INSERT",
            "outbox_events": "INSERT",
            "audit_entries": "INSERT",
        },
        "internal": {"matches": "SELECT"},
        "worker": {
            "catalog": "SELECT,INSERT,UPDATE",
            "item_vectors": "SELECT,INSERT,UPDATE",
            "reports": "SELECT,UPDATE",
            "matches": "SELECT,INSERT,UPDATE",
            "work_jobs": "SELECT,INSERT,UPDATE",
            "outbox_events": "SELECT,INSERT,UPDATE",
            "inbox_receipts": "SELECT,INSERT",
            "audit_entries": "INSERT",
        },
    }
    for mode, tables in permissions.items():
        for table, permission in tables.items():
            connection.execute(text(f"GRANT {permission} ON matching.{table} TO {quoted[mode]}"))
    connection.execute(text(f"GRANT UPDATE(active) ON matching.matches TO {quoted['api']}"))
    connection.execute(
        text(
            "GRANT SELECT(id,state,category,venue_id,found_from,found_until) ON matching.reports "
            f"TO {quoted['internal']}"
        )
    )
    for table in ["audit_entries", "work_jobs", "outbox_events"]:
        columns = "id,created_at" + (",available_at" if table != "audit_entries" else "")
        connection.execute(text(f"GRANT SELECT({columns}) ON matching.{table} TO {quoted['api']}"))
    connection.execute(
        text(f"GRANT SELECT(id,created_at) ON matching.audit_entries TO {quoted['worker']}")
    )


engine = create_engine(settings.migration_database_url.get_secret_value(), poolclass=pool.NullPool)
try:
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(hashtext('lostlink:matching:migration'))"))
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS matching"))
        connection.commit()
        try:
            context.configure(
                connection=connection,
                target_metadata=Base.metadata,
                version_table_schema="matching",
                include_schemas=True,
                include_name=include_name,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
                grants(connection)
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(hashtext('lostlink:matching:migration'))")
            )
            connection.commit()
finally:
    engine.dispose()
