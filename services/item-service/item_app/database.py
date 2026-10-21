"""Transaction-local contexts, including a single-resource internal locator."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from lostlink_common.db import Database, check_runtime_role
from sqlalchemy import event, text
from sqlalchemy.orm import Session


class ItemDatabase(Database):
    @contextmanager
    def context(
        self, *, subject: str = "", tenant_id: UUID | None = None, resource_id: UUID | None = None
    ) -> Iterator[Session]:
        with Session(self.engine, expire_on_commit=False) as session:

            @event.listens_for(session, "after_begin")
            def set_context(_session, _transaction, connection):
                check_runtime_role(connection)
                connection.execute(
                    text(
                        "SELECT set_config('app.user_sub', :subject, true), "
                        "set_config('app.tenant_id', :tenant, true), "
                        "set_config('app.resource_id', :resource, true)"
                    ),
                    {
                        "subject": subject,
                        "tenant": str(tenant_id) if tenant_id else "",
                        "resource": str(resource_id) if resource_id else "",
                    },
                )

            with session.begin():
                yield session
