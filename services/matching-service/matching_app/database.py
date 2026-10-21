"""Owner context is transaction local; a worker credential is a separate access mode."""

from contextlib import contextmanager

from lostlink_common.db import Database, check_runtime_role
from sqlalchemy import event, text
from sqlalchemy.orm import Session


class MatchingDatabase(Database):
    @contextmanager
    def context(self, *, subject="", resource_id=None):
        with Session(self.engine, expire_on_commit=False) as session:

            @event.listens_for(session, "after_begin")
            def set_context(_session, _transaction, connection):
                check_runtime_role(connection)
                connection.execute(
                    text(
                        "SELECT set_config('app.user_sub', :subject, true), "
                        "set_config('app.resource_id', :resource, true)"
                    ),
                    {"subject": subject, "resource": str(resource_id) if resource_id else ""},
                )

            with session.begin():
                yield session
