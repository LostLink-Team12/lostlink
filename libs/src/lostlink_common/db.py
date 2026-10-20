"""PostgreSQL transactions with fail-closed, transaction-local tenant context."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException, Request
from sqlalchemy import DateTime, Engine, create_engine, event, func, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Session, mapped_column

from lostlink_common.auth import require_tenant
from lostlink_common.config import Settings


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
        raise ValueError("SQL identifiers must use lowercase letters, digits and underscores")
    return f'"{value}"'


def enable_tenant_rls(connection: Connection, schema: str, table: str) -> None:
    """Use in migrations after creating a table with a non-null tenant_id UUID."""
    qualified = f"{identifier(schema)}.{identifier(table)}"
    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    connection.execute(text(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"))
    connection.execute(text(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY"))
    connection.execute(
        text(
            f"CREATE POLICY tenant_isolation ON {qualified} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )


def check_runtime_role(connection: Connection) -> None:
    role = connection.execute(
        text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).one()
    if role.rolsuper or role.rolbypassrls:
        raise RuntimeError("Application database role must not bypass row-level security")


class TenantRecord:
    """Common columns; each service owns its own DeclarativeBase and schema."""

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Database:
    def __init__(self, settings: Settings):
        if settings.database_url is None:
            raise ValueError("Database configuration is required")
        self.engine: Engine = create_engine(
            settings.database_url.get_secret_value(),
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=3,
            connect_args={
                "connect_timeout": 3,
                "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
            },
        )

    @contextmanager
    def session(self, tenant_id: UUID) -> Iterator[Session]:
        if not isinstance(tenant_id, UUID):
            raise ValueError("An explicit tenant UUID is required")
        with Session(self.engine, expire_on_commit=False) as session:
            # The listener also applies to a new transaction after a caller rollback.
            # Never use connection-level SET: pooled connections serve multiple tenants.
            @event.listens_for(session, "after_begin")
            def set_tenant(_session, _transaction, connection):
                check_runtime_role(connection)
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )

            with session.begin():
                yield session

    def check_ready(self, schema: str, revision: str) -> None:
        with self.engine.connect() as connection:
            check_runtime_role(connection)
            current_revision = connection.execute(
                text(f"SELECT version_num FROM {identifier(schema)}.alembic_version")
            ).scalar_one()
            if current_revision != revision:
                raise RuntimeError("Database migration is not current")

    def close(self) -> None:
        self.engine.dispose()


def get_tenant_session(
    request: Request, tenant_id: Annotated[UUID, Depends(require_tenant)]
) -> Iterator[Session]:
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(503, "Database is not configured")
    with database.session(tenant_id) as session:
        yield session
