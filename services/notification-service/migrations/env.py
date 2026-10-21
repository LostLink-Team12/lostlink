"""Notification-only DDL with three independent non-owner runtime roles."""

from alembic import context
from lostlink_common.db import identifier
from notification_app.models import Base
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, pool, text


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    migration_database_url: SecretStr
    notification_api_role: str = "lostlink_notification_api"
    notification_admin_role: str = "lostlink_notification_admin"
    notification_worker_role: str = "lostlink_notification_worker"


settings = MigrationSettings()
roles = {
    mode: getattr(settings, f"notification_{mode}_role") for mode in ["api", "admin", "worker"]
}
if len(set(roles.values())) != 3:
    raise ValueError("Notification database roles must be distinct")
quoted = {mode: identifier(role) for mode, role in roles.items()}
if context.is_offline_mode():
    raise RuntimeError("Use online migrations to validate grants")


def include_name(name, type_, _parent_names):
    return name == "notification" if type_ == "schema" else True


OWNER = "owner_sub = NULLIF(current_setting('app.user_sub', true), '')"
ACTOR = "actor_sub = NULLIF(current_setting('app.user_sub', true), '')"
JOB_COLUMNS = "state,attempts,available_at,lease_token,lease_until,last_error_code"

POLICIES = {
    "notifications": {"api": OWNER, "admin": "true", "worker": "true"},
    "notification_jobs": {"admin": "true", "worker": "true"},
    "notification_attempts": {"admin": "true", "worker": "true"},
    "inbox_receipts": {"worker": "true"},
    "idempotency_records": {"admin": ACTOR},
    "audit_entries": {"admin": ACTOR, "worker": "true"},
}
PERMISSIONS = {
    "api": {"notifications": "SELECT"},
    "admin": {
        "notifications": "SELECT",
        "notification_jobs": "SELECT",
        "notification_attempts": "SELECT",
        "idempotency_records": "SELECT,INSERT,DELETE",
        "audit_entries": "INSERT",
    },
    "worker": {
        "notifications": "SELECT,INSERT",
        "notification_jobs": "SELECT,INSERT",
        "notification_attempts": "SELECT,INSERT",
        "inbox_receipts": "SELECT,INSERT",
        "audit_entries": "INSERT",
    },
}
# Identity, ownership and match binding columns are never updatable at runtime.
COLUMN_UPDATES = {
    "admin": {"notifications": "state,reason_code", "notification_jobs": JOB_COLUMNS},
    "worker": {
        "notifications": "state,reason_code,sent_at",
        "notification_jobs": JOB_COLUMNS,
        "notification_attempts": "finished_at,result,provider_message_id,error_code",
    },
}


def grants(connection):
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
        connection.execute(text(f"GRANT USAGE ON SCHEMA notification TO {quoted[mode]}"))
        connection.execute(text(f"GRANT SELECT ON notification.alembic_version TO {quoted[mode]}"))
    if not connection.scalar(text("SELECT to_regclass('notification.notifications')")):
        return
    for table, modes in POLICIES.items():
        qualified = f"notification.{table}"
        connection.execute(text(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"))
        connection.execute(text(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY"))
        connection.execute(text(f"REVOKE ALL ON {qualified} FROM PUBLIC"))
        for mode, predicate in modes.items():
            name = identifier(f"{table}_{mode}")
            connection.execute(text(f"DROP POLICY IF EXISTS {name} ON {qualified}"))
            connection.execute(
                text(
                    f"CREATE POLICY {name} ON {qualified} TO {quoted[mode]} "
                    f"USING ({predicate}) WITH CHECK ({predicate})"
                )
            )
    for mode, tables in PERMISSIONS.items():
        for table, permission in tables.items():
            connection.execute(
                text(f"GRANT {permission} ON notification.{table} TO {quoted[mode]}")
            )
        for table, columns in COLUMN_UPDATES.get(mode, {}).items():
            connection.execute(
                text(f"GRANT UPDATE({columns}) ON notification.{table} TO {quoted[mode]}")
            )
    for mode in ["admin", "worker"]:
        # INSERT ... RETURNING reads server defaults only.
        connection.execute(
            text(f"GRANT SELECT(id,created_at) ON notification.audit_entries TO {quoted[mode]}")
        )


engine = create_engine(settings.migration_database_url.get_secret_value(), poolclass=pool.NullPool)
try:
    with engine.connect() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS notification"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table_schema="notification",
            include_schemas=True,
            include_name=include_name,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
            grants(connection)
finally:
    engine.dispose()
