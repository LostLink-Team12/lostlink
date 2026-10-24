"""Claim-only DDL with two independent non-owner runtime roles."""

from alembic import context
from claim_app.models import Base
from lostlink_common.db import identifier
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, pool, text


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    migration_database_url: SecretStr
    claim_api_role: str = "lostlink_claim_api"
    claim_worker_role: str = "lostlink_claim_worker"


settings = MigrationSettings()
roles = {mode: getattr(settings, f"claim_{mode}_role") for mode in ["api", "worker"]}
if len(set(roles.values())) != 2:
    raise ValueError("Claim database roles must be distinct")
quoted = {mode: identifier(role) for mode, role in roles.items()}
if context.is_offline_mode():
    raise RuntimeError("Use online migrations to validate grants")


def include_name(name, type_, _parent_names):
    return name == "claim" if type_ == "schema" else True


TENANT = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
OWNER = "owner_sub = NULLIF(current_setting('app.user_sub', true), '')"
ACTOR = "actor_sub = NULLIF(current_setting('app.user_sub', true), '')"
OWNER_OR_TENANT = f"({OWNER}) OR ({TENANT})"
# Venue staff can contact claimants during review and pending handover.
CONTACT = (
    f"({OWNER}) OR (({TENANT}) AND EXISTS (SELECT 1 FROM claim.claims c "
    "WHERE c.id = claim_id AND c.state IN "
    "('submitted','approval_pending','approved','handover_pending')))"
)

POLICIES = {
    "claims": {"api": OWNER_OR_TENANT, "worker": TENANT},
    "claim_answers": {"api": OWNER_OR_TENANT},
    "claim_contacts": {"api": CONTACT},
    "claim_operations": {"api": OWNER_OR_TENANT, "worker": "true"},
    "handover_tokens": {"api": OWNER_OR_TENANT, "worker": TENANT},
    "handover_records": {"api": OWNER_OR_TENANT, "worker": TENANT},
    # API rows are insert-only; it cannot read event bodies or acknowledge delivery.
    "outbox_events": {"api": "true", "worker": "true"},
    "idempotency_records": {"api": ACTOR},
    "audit_entries": {"api": ACTOR, "worker": "true"},
}

PERMISSIONS = {
    "api": {
        "claims": "SELECT,INSERT",
        "claim_answers": "SELECT,INSERT",
        "claim_contacts": "SELECT,INSERT",
        "claim_operations": "SELECT,INSERT",
        "handover_tokens": "SELECT,INSERT",
        "handover_records": "SELECT,INSERT",
        "outbox_events": "INSERT",
        "idempotency_records": "SELECT,INSERT,DELETE",
        "audit_entries": "INSERT",
    },
    "worker": {
        "claims": "SELECT",
        "claim_operations": "SELECT",
        "handover_tokens": "SELECT",
        "handover_records": "SELECT",
        "outbox_events": "SELECT,INSERT,UPDATE",
        "audit_entries": "INSERT",
    },
}

# Identity, tenant and ownership columns are never updatable by runtime roles.
COLUMN_UPDATES = {
    "api": {
        "claims": "state,version,reviewed_by,decision_code,approved_at,closed_at",
        "claim_contacts": "key_id,encrypted_contact",
        "claim_operations": "kind,available_at",
        "handover_tokens": "state,handover_id,replay_key_id,replay_ciphertext,replay_expires_at",
    },
    "worker": {
        "claims": "state,version,decision_code,approved_at,closed_at",
        "claim_operations": (
            "state,attempts,available_at,lease_token,lease_until,last_error_code,"
            "result_code,completed_at,deadline_at"
        ),
        "handover_tokens": "state,replay_key_id,replay_ciphertext,replay_expires_at",
        "handover_records": "state,completed_at",
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
        connection.execute(text(f"GRANT USAGE ON SCHEMA claim TO {quoted[mode]}"))
        connection.execute(text(f"GRANT SELECT ON claim.alembic_version TO {quoted[mode]}"))
    if not connection.scalar(text("SELECT to_regclass('claim.claims')")):
        return
    for table, modes in POLICIES.items():
        connection.execute(text(f"ALTER TABLE claim.{table} ENABLE ROW LEVEL SECURITY"))
        connection.execute(text(f"ALTER TABLE claim.{table} FORCE ROW LEVEL SECURITY"))
        connection.execute(text(f"REVOKE ALL ON claim.{table} FROM PUBLIC"))
        for mode, predicate in modes.items():
            name = identifier(f"{table}_{mode}")
            connection.execute(text(f"DROP POLICY IF EXISTS {name} ON claim.{table}"))
            connection.execute(
                text(
                    f"CREATE POLICY {name} ON claim.{table} TO {quoted[mode]} "
                    f"USING ({predicate}) WITH CHECK ({predicate})"
                )
            )
    for mode, tables in PERMISSIONS.items():
        for table, permission in tables.items():
            connection.execute(text(f"GRANT {permission} ON claim.{table} TO {quoted[mode]}"))
        for table, columns in COLUMN_UPDATES[mode].items():
            connection.execute(text(f"GRANT UPDATE({columns}) ON claim.{table} TO {quoted[mode]}"))
    for mode in roles:
        # INSERT ... RETURNING reads server defaults; no event body or audit detail.
        for table in ["audit_entries", "outbox_events"]:
            connection.execute(
                text(f"GRANT SELECT(id,created_at) ON claim.{table} TO {quoted[mode]}")
            )
        connection.execute(
            text(f"GRANT SELECT(available_at) ON claim.outbox_events TO {quoted[mode]}")
        )


engine = create_engine(settings.migration_database_url.get_secret_value(), poolclass=pool.NullPool)
try:
    with engine.connect() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS claim"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table_schema="claim",
            include_schemas=True,
            include_name=include_name,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
            grants(connection)
finally:
    engine.dispose()
