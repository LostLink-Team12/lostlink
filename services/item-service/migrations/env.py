"""Item-only DDL with three independent non-owner runtime roles."""

from alembic import context
from item_app.models import Base
from lostlink_common.db import identifier
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, pool, text


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    migration_database_url: SecretStr
    item_api_role: str = "lostlink_item_api"
    item_internal_role: str = "lostlink_item_internal"
    item_worker_role: str = "lostlink_item_worker"


settings = MigrationSettings()
roles = {mode: getattr(settings, f"item_{mode}_role") for mode in ["api", "internal", "worker"]}
if len(set(roles.values())) != 3:
    raise ValueError("Item database roles must be distinct")
quoted = {mode: identifier(role) for mode, role in roles.items()}
if context.is_offline_mode():
    raise RuntimeError("Use online migrations to validate grants")


def include_name(name, type_, _parent_names):
    return name == "item" if type_ == "schema" else True


def grants(connection):
    tenant = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    actor = "actor_sub = NULLIF(current_setting('app.user_sub', true), '')"
    resource = "item_id = NULLIF(current_setting('app.resource_id', true), '')::uuid"
    for mode, role in roles.items():
        flags = connection.execute(
            text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=:name"), {"name": role}
        ).one()
        if (
            flags.rolsuper
            or flags.rolbypassrls
            or connection.scalar(text("SELECT current_user")) == role
        ):
            raise ValueError("Runtime roles must be non-owner and cannot bypass RLS")
        connection.execute(text(f"GRANT USAGE ON SCHEMA item TO {quoted[mode]}"))
        connection.execute(text(f"GRANT SELECT ON item.alembic_version TO {quoted[mode]}"))
    if not connection.scalar(text("SELECT to_regclass('item.items')")):
        return
    domain = [
        "items",
        "verification_questions",
        "item_photos",
        "reservations",
        "import_jobs",
        "import_entries",
    ]
    policies = {table: {"api": tenant, "worker": tenant} for table in domain}
    for table in ["items", "verification_questions", "item_photos", "reservations"]:
        policies[table]["internal"] = tenant
    policies.update(
        {
            "item_locator": {
                "api": tenant,
                "internal": resource,
                "worker": f"({resource}) OR ({tenant})",
            },
            "work_jobs": {"api": tenant, "internal": tenant, "worker": "true"},
            "outbox_events": {"api": tenant, "internal": tenant, "worker": "true"},
            "inbox_receipts": {"worker": "true"},
            "idempotency_records": {
                "api": f"({tenant}) AND ({actor})",
                "internal": f"({tenant}) AND ({actor})",
            },
            "audit_entries": {
                "api": f"({tenant}) AND ({actor})",
                "internal": f"({tenant}) AND ({actor})",
                "worker": actor,
            },
        }
    )
    for table, modes in policies.items():
        connection.execute(text(f"ALTER TABLE item.{table} ENABLE ROW LEVEL SECURITY"))
        connection.execute(text(f"ALTER TABLE item.{table} FORCE ROW LEVEL SECURITY"))
        connection.execute(text(f"REVOKE ALL ON item.{table} FROM PUBLIC"))
        for mode, predicate in modes.items():
            name = identifier(f"{table}_{mode}")
            connection.execute(text(f"DROP POLICY IF EXISTS {name} ON item.{table}"))
            connection.execute(
                text(
                    f"CREATE POLICY {name} ON item.{table} TO {quoted[mode]} "
                    f"USING ({predicate}) WITH CHECK ({predicate})"
                )
            )
    permissions = {
        "api": {
            **dict.fromkeys(domain, "SELECT,INSERT,UPDATE"),
            "verification_questions": "SELECT,INSERT,UPDATE,DELETE",
            "item_locator": "INSERT",
            "work_jobs": "SELECT,INSERT",
            "outbox_events": "INSERT",
            "audit_entries": "INSERT",
            "idempotency_records": "SELECT,INSERT,DELETE",
        },
        "internal": {
            "items": "SELECT",
            "item_locator": "SELECT",
            "item_photos": "SELECT",
            "reservations": "SELECT,INSERT,UPDATE",
            "work_jobs": "INSERT",
            "outbox_events": "INSERT",
            "audit_entries": "INSERT",
            "idempotency_records": "SELECT,INSERT,DELETE",
        },
        "worker": {
            **dict.fromkeys(domain, "SELECT,INSERT,UPDATE"),
            "verification_questions": "SELECT,INSERT",
            "item_locator": "SELECT,INSERT",
            "work_jobs": "SELECT,UPDATE",
            "outbox_events": "SELECT,INSERT,UPDATE",
            "inbox_receipts": "SELECT,INSERT",
            "audit_entries": "INSERT",
        },
    }
    for mode, tables in permissions.items():
        if mode == "api":
            tables.pop("reservations", None)
        if mode == "worker":
            tables["reservations"] = "SELECT"
        for table, permission in tables.items():
            connection.execute(text(f"GRANT {permission} ON item.{table} TO {quoted[mode]}"))
    connection.execute(
        text(f"GRANT UPDATE(state,version,reserved_claim_id) ON item.items TO {quoted['internal']}")
    )
    connection.execute(
        text(
            "GRANT SELECT(id,tenant_id,item_id,prompt,position,version) "
            f"ON item.verification_questions TO {quoted['internal']}"
        )
    )
    for mode in roles:
        for table in ["audit_entries", "outbox_events", "work_jobs"]:
            connection.execute(
                text(f"GRANT SELECT(id,created_at) ON item.{table} TO {quoted[mode]}")
            )
        for table in ["outbox_events", "work_jobs"]:
            connection.execute(
                text(f"GRANT SELECT(available_at) ON item.{table} TO {quoted[mode]}")
            )


engine = create_engine(settings.migration_database_url.get_secret_value(), poolclass=pool.NullPool)
try:
    with engine.connect() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS item"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table_schema="item",
            include_schemas=True,
            include_name=include_name,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
            grants(connection)
finally:
    engine.dispose()
