"""One random database, the two restricted Delivery roles, the real API and the worker."""

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from delivery_app.config import PARTNER_SCOPE, DeliverySettings
from delivery_app.main import create_app
from delivery_app.workers import DeliveryWorker
from fastapi.testclient import TestClient
from lostlink_common.auth import InvalidToken, Principal
from lostlink_common.config import Settings
from lostlink_common.crypto import FieldCipher
from lostlink_common.db import Database
from psycopg import sql
from sqlalchemy.engine import make_url

IDENTITIES = {
    "owner": Principal(subject="owner-1", client_id="owner-web", roles={"owner"}),
    "other": Principal(subject="owner-2", client_id="owner-web", roles={"owner"}),
    "courier-a": Principal(subject="courier-a", client_id="courier-a", scopes={PARTNER_SCOPE}),
    "courier-b": Principal(subject="courier-b", client_id="courier-b", scopes={PARTNER_SCOPE}),
}


class Verifier:
    def verify(self, token):
        if token not in IDENTITIES:
            raise InvalidToken()
        return IDENTITIES[token]


class Publisher:
    def __init__(self):
        self.events, self.failures = [], 0

    def publish(self, event):
        if self.failures:
            self.failures -= 1
            raise OSError("injected SNS outage")
        self.events.append(event)


@pytest.fixture
def delivery_env():
    admin_url = os.getenv("LOSTLINK_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set LOSTLINK_TEST_ADMIN_URL to a local PostgreSQL administrator URL")
    suffix, password = uuid4().hex, uuid4().hex
    base = make_url(admin_url)
    name = f"ll_delivery_test_{suffix}"
    roles = {mode: f"ll_d_{mode}_{suffix}" for mode in ["api", "worker"]}
    dbs, created, database_created = {}, [], False
    admin_dsn = base.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        try:
            for role in roles.values():
                admin.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS "
                        "NOCREATEDB NOCREATEROLE NOINHERIT"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
                created.append(role)
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            database_created = True
            migration_url = base.set(database=name).render_as_string(hide_password=False)
            config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("MIGRATION_DATABASE_URL", migration_url)
                for mode, role in roles.items():
                    patch.setenv(f"DELIVERY_{mode.upper()}_ROLE", role)
                command.upgrade(config, "head")
                command.upgrade(config, "head")  # re-running applies grants idempotently
            for mode, role in roles.items():
                url = base.set(database=name, username=role, password=password)
                dbs[mode] = Database(
                    Settings(_env_file=None, database_url=url.render_as_string(hide_password=False))
                )
            cipher = FieldCipher.from_secret(f'{{"d1": "{FieldCipher.generate_key()}"}}', "d1")
            settings = DeliverySettings(
                _env_file=None,
                user_client_ids=["owner-web"],
                partner_client_ids=["courier-a", "courier-b"],
            )
            app = create_app(
                settings, token_verifier=Verifier(), database=dbs["api"], cipher=cipher
            )
            publisher = Publisher()
            with TestClient(app) as http:
                yield SimpleNamespace(
                    client=http,
                    db=dbs,
                    worker=DeliveryWorker(dbs["worker"]),
                    publisher_worker=DeliveryWorker(dbs["worker"], publisher=publisher),
                    publisher=publisher,
                    tenant=uuid4(),
                )
        finally:
            for db in dbs.values():
                db.close()
            if database_created:
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
            for role in created:
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
