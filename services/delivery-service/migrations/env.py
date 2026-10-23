"""Delivery-only DDL with two independent non-owner runtime roles (api, worker)."""

from alembic import context
from delivery_app.models import Base
from lostlink_common.db import identifier
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, pool, text


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    migration_database_url: SecretStr
    delivery_api_role: str = "lostlink_delivery_api"
    delivery_worker_role: str = "lostlink_delivery_worker"


settings = MigrationSettings()
roles = {mode: getattr(settings, f"delivery_{mode}_role") for mode in ["api", "worker"]}
if len(set(roles.values())) != 2:
    raise ValueError("Delivery database roles must be distinct")
quoted = {mode: identifier(role) for mode, role in roles.items()}
if context.is_offline_mode():
    raise RuntimeError("Use online migrations to validate grants")


def include_name(name, type_, _parent_names):
    return name == "delivery" if type_ == "schema" else True


SUBJECT = "NULLIF(current_setting('app.user_sub', true), '')"
OWNER = f"owner_sub = {SUBJECT}"
# Partners are machine clients; their token subject is the client ID stored as partner_id.
OWNER_OR_PARTNER = f"(owner_sub = {SUBJECT} OR partner_id = {SUBJECT})"
ACTOR = f"actor_sub = {SUBJECT}"
LIFECYCLE = "state,version,delivery_attempts,updated_at"
LEASE = "state,attempts,available_at,lease_token,lease_until,last_error_code"

POLICIES = {
    "claim_approvals": {"api": OWNER, "worker": "true"},
    "shipments": {"api": OWNER_OR_PARTNER, "worker": "true"},
    "shipment_events": {"api": OWNER_OR_PARTNER, "worker": "true"},
    "outbox_events": {"api": "true", "worker": "true"},
    "inbox_receipts": {"worker": "true"},
    "idempotency_records": {"api": ACTOR},
}
PERMISSIONS = {
    "api": {
        "claim_approvals": "SELECT",
        "shipments": "SELECT,INSERT",
        "shipment_events": "SELECT,INSERT",
        "outbox_events": "INSERT",
        "idempotency_records": "SELECT,INSERT,DELETE",
    },
    "worker": {
        "claim_approvals": "SELECT,INSERT",
        "shipments": "SELECT",
        "shipment_events": "SELECT,INSERT",
        "outbox_events": "SELECT,INSERT",
        "inbox_receipts": "SELECT,INSERT",
    },
}
# Ownership, partner, claim and address columns are never updatable at runtime.
COLUMN_UPDATES = {
    "api": {"shipments": LIFECYCLE},
    "worker": {
        "claim_approvals": "state,updated_at",
        "shipments": LIFECYCLE,
        "outbox_events": LEASE,
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
        connection.execute(text(f"GRANT USAGE ON SCHEMA delivery TO {quoted[mode]}"))
        connection.execute(text(f"GRANT SELECT ON delivery.alembic_version TO {quoted[mode]}"))
    if not connection.scalar(text("SELECT to_regclass('delivery.shipments')")):
        return
    for table, modes in POLICIES.items():
        qualified = f"delivery.{table}"
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
            connection.execute(text(f"GRANT {permission} ON delivery.{table} TO {quoted[mode]}"))
        for table, columns in COLUMN_UPDATES.get(mode, {}).items():
            connection.execute(
                text(f"GRANT UPDATE({columns}) ON delivery.{table} TO {quoted[mode]}")
            )
    # INSERT ... RETURNING reads server defaults only.
    connection.execute(
        text(
            f"GRANT SELECT(id,created_at,available_at) ON delivery.outbox_events TO {quoted['api']}"
        )
    )


engine = create_engine(settings.migration_database_url.get_secret_value(), poolclass=pool.NullPool)
try:
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(hashtext('lostlink:delivery:migration'))"))
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS delivery"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table_schema="delivery",
            include_schemas=True,
            include_name=include_name,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
            grants(connection)
finally:
    engine.dispose()
