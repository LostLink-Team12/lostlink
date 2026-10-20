"""PostgreSQL transactions with fail-closed, transaction-local access context."""

import re
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException, Request
from sqlalchemy import DateTime, Engine, create_engine, event, func, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Session, mapped_column

from lostlink_common.auth import Principal, require_roles, require_tenant, validate_subject
from lostlink_common.config import Settings

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
        raise ValueError("SQL identifiers must use lowercase letters, digits and underscores")
    return f'"{value}"'


def enable_tenant_rls(connection: Connection, schema: str, table: str) -> None:
    """Use in migrations after creating a table with a non-null tenant_id UUID."""
    qualified = f"{identifier(schema)}.{identifier(table)}"
    connection.execute(text(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"))
    connection.execute(text(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY"))
    connection.execute(
        text(
            f"CREATE POLICY tenant_isolation ON {qualified} "
            f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
        )
    )


def enable_owner_or_tenant_rls(
    connection: Connection, schema: str, table: str, *, owner_column: str = "owner_sub"
) -> None:
    """Rows are visible to their owner or to the current venue context.

    PostgreSQL ORs permissive policies, so a venue actor who is also the owner sees both
    sets. Restrict INSERT/UPDATE columns with grants: these policies alone let an owner
    write a row for any tenant and venue staff write a row for any owner in their venue.
    """
    qualified = f"{identifier(schema)}.{identifier(table)}"
    owner = f"{identifier(owner_column)} = NULLIF(current_setting('app.user_sub', true), '')"
    connection.execute(text(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"))
    connection.execute(text(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY"))
    connection.execute(
        text(f"CREATE POLICY owner_access ON {qualified} USING ({owner}) WITH CHECK ({owner})")
    )
    connection.execute(
        text(
            f"CREATE POLICY tenant_access ON {qualified} "
            f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
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
    def scoped_session(
        self,
        *,
        subject: str = "",
        tenant_id: UUID | None = None,
        resource_id: UUID | None = None,
    ) -> Iterator[Session]:
        """Open one transaction with an explicit, transaction-local access context.

        Pass only values derived from a verified principal, a trusted resource locator or
        a trusted event. An empty context is not "all tenants": RLS then returns no rows.
        """
        validate_subject(subject, allow_empty=True)
        for value in (tenant_id, resource_id):
            if value is not None and not isinstance(value, UUID):
                raise ValueError("Tenant and resource context values must be UUIDs")
        settings = {
            "subject": subject,
            "tenant": str(tenant_id) if tenant_id else "",
            "resource": str(resource_id) if resource_id else "",
        }
        with Session(self.engine, expire_on_commit=False) as session:
            # The listener also applies to a new transaction after a caller rollback.
            # Never use connection-level SET: pooled connections serve multiple actors.
            @event.listens_for(session, "after_begin")
            def set_context(_session, _transaction, connection):
                check_runtime_role(connection)
                connection.execute(
                    text(
                        "SELECT set_config('app.user_sub', :subject, true), "
                        "set_config('app.tenant_id', :tenant, true), "
                        "set_config('app.resource_id', :resource, true)"
                    ),
                    settings,
                )

            with session.begin():
                yield session

    @contextmanager
    def session(self, tenant_id: UUID) -> Iterator[Session]:
        if not isinstance(tenant_id, UUID):
            raise ValueError("An explicit tenant UUID is required")
        with self.scoped_session(tenant_id=tenant_id) as session:
            yield session

    def actor_session(
        self, principal: Principal, *, venue_scope: bool
    ) -> AbstractContextManager[Session]:
        """Owner mode (venue_scope=False) sets only the subject, never a tenant."""
        if venue_scope and principal.tenant_id is None:
            raise ValueError("Venue-scoped sessions require a verified tenant")
        return self.scoped_session(
            subject=validate_subject(principal.subject),
            tenant_id=principal.tenant_id if venue_scope else None,
        )

    def system_session(self) -> AbstractContextManager[Session]:
        """Worker access to non-RLS operational tables; the role grants define the scope."""
        return self.scoped_session()

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


def _request_database(request: Request) -> Database:
    database: Database | None = request.app.state.database
    if database is None:
        raise HTTPException(503, "Database is not configured")
    return database


def get_tenant_session(
    request: Request, tenant_id: Annotated[UUID, Depends(require_tenant)]
) -> Iterator[Session]:
    with _request_database(request).session(tenant_id) as session:
        yield session


def get_owner_session(
    request: Request, principal: Annotated[Principal, Depends(require_roles("owner"))]
) -> Iterator[Session]:
    """Owner data access; machine tokens carry no owner group and are rejected."""
    with _request_database(request).actor_session(principal, venue_scope=False) as session:
        yield session
