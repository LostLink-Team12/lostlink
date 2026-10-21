"""Explicit transaction context; permission comes from SQL roles and RLS."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from lostlink_common.db import Database, check_runtime_role
from sqlalchemy import event, text
from sqlalchemy.orm import Session


class VenueDatabase(Database):
    @contextmanager
    def context(self, *, subject: str = "", tenant_id: UUID | None = None) -> Iterator[Session]:
        with Session(self.engine, expire_on_commit=False) as session:

            @event.listens_for(session, "after_begin")
            def set_context(_session, _transaction, connection):
                check_runtime_role(connection)
                connection.execute(
                    text(
                        "SELECT set_config('app.user_sub', :subject, true), "
                        "set_config('app.tenant_id', :tenant, true)"
                    ),
                    {"subject": subject, "tenant": str(tenant_id) if tenant_id else ""},
                )

            with session.begin():
                yield session
