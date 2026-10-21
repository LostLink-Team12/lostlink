"""One random database with real Item and Matching apps feeding Notification."""

import io
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from item_app.config import ItemSettings
from item_app.database import ItemDatabase
from item_app.main import create_app as create_item_app
from item_app.workers import ItemWorker
from lostlink_common.auth import InvalidToken, Principal
from lostlink_common.config import Settings
from lostlink_common.db import Database
from lostlink_common.identity import StaticIdentityDirectory
from matching_app.clients import ItemClient as MatchingItemClient
from matching_app.config import MatchingSettings
from matching_app.database import MatchingDatabase
from matching_app.main import create_app as create_matching_app
from matching_app.workers import MatchingWorker
from notification_app.clients import MatchingClient
from notification_app.config import NotificationSettings
from notification_app.mail import CaptureSender
from notification_app.main import create_app
from notification_app.workers import NotificationWorker
from psycopg import sql
from sqlalchemy.engine import make_url

SERVICES = {
    "item": ["api", "internal", "worker"],
    "matching": ["api", "internal", "worker"],
    "notification": ["api", "admin", "worker"],
}
OWNER_APP = "https://app.lostlink.example"


class Encoder:
    model_version = "test-clip-512"

    def ready(self):
        pass

    def encode(self, text=None, photo=None):
        value = [0.0] * 512
        value[0 if text and "red" in text.lower() else 1] = 1.0
        return value


class Storage:
    content = b"valid test photo"

    def ready(self, *_):
        pass

    def presign(self, *_):
        return {"url": "https://storage.invalid", "fields": {}}

    def seal(self, *_):
        return '"sealed"'

    def photo_url(self, *_):
        return "https://storage.invalid/signed-photo"

    def get_object(self, **_):
        return {"Body": io.BytesIO(self.content), "ContentLength": len(self.content)}


class Membership:
    def require_current(self, *_):
        pass


class MatchingTransport:
    def __init__(self, client):
        self.client, self.failures = client, 0

    def __call__(self, url, headers):
        if self.failures:
            self.failures -= 1
            raise OSError("injected Matching outage")
        response = self.client.get(urlsplit(url).path, headers=headers)
        return response.status_code, response.json() if response.content else None


@pytest.fixture
def notification_env():
    admin_url = os.getenv("LOSTLINK_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set LOSTLINK_TEST_ADMIN_URL to a local PostgreSQL administrator URL")
    suffix, password = uuid4().hex, uuid4().hex
    base = make_url(admin_url)
    name = f"ll_notification_test_{suffix}"
    roles = {
        f"{svc}_{mode}": f"ll_{svc[0]}_{mode}_{suffix}"
        for svc, modes in SERVICES.items()
        for mode in modes
    }
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
            root, configs = Path(__file__).parents[3], {}
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("MIGRATION_DATABASE_URL", migration_url)
                for key, role in roles.items():
                    patch.setenv(key.upper() + "_ROLE", role)
                for svc in SERVICES:
                    config = Config(str(root / f"services/{svc}-service/alembic.ini"))
                    configs[svc] = config
                    command.upgrade(config, "head")
                    command.upgrade(config, "head")
            classes = {"item": ItemDatabase, "matching": MatchingDatabase, "notification": Database}
            for key, role in roles.items():
                url = base.set(database=name, username=role, password=password)
                dbs[key] = classes[key.split("_")[0]](
                    Settings(_env_file=None, database_url=url.render_as_string(hide_password=False))
                )
            tenants = {"a": uuid4()}
            identities = {
                "owner": Principal(subject="owner", client_id="web", roles={"owner"}),
                "other": Principal(subject="other", client_id="web", roles={"owner"}),
                "a": Principal(
                    subject="staff-a",
                    client_id="web",
                    tenant_id=tenants["a"],
                    roles={"venue_staff", "owner"},
                ),
                "index": Principal(
                    subject="matching", client_id="index", scopes={"lostlink/items.index"}
                ),
                "notify": Principal(
                    subject="notification",
                    client_id="notify",
                    scopes={"lostlink/matches.notify"},
                ),
                "operator": Principal(
                    subject="operator-1", client_id="ops", roles={"platform_admin"}
                ),
                "fake-operator": Principal(
                    subject="operator-2", client_id="web", roles={"platform_admin", "owner"}
                ),
            }

            class Verifier:
                def verify(self, token):
                    if token not in identities:
                        raise InvalidToken()
                    return identities[token]

            storage = Storage()
            item_app = create_item_app(
                ItemSettings(
                    _env_file=None,
                    user_client_ids=["web"],
                    index_client_ids=["index"],
                    photo_bucket="test-photos",
                ),
                database=dbs["item_api"],
                internal_database=dbs["item_internal"],
                token_verifier=Verifier(),
                membership_client=Membership(),
                storage=storage,
            )
            with TestClient(item_app) as item_http:

                def item_transport(url, headers):
                    response = item_http.get(urlsplit(url).path, headers=headers)
                    if response.status_code == 404:
                        return None
                    assert response.status_code == 200, response.text
                    return response.json()

                item_client = MatchingItemClient(
                    "https://item.invalid", lambda: "index", transport=item_transport
                )
                matching_settings = MatchingSettings(
                    _env_file=None,
                    user_client_ids=["web"],
                    notification_client_ids=["notify"],
                    item_photo_buckets=["test-photos"],
                    match_threshold=0.5,
                )
                encoder = Encoder()
                matching_app = create_matching_app(
                    matching_settings,
                    database=dbs["matching_api"],
                    internal_database=dbs["matching_internal"],
                    token_verifier=Verifier(),
                    encoder=encoder,
                    item_client=item_client,
                )
                with TestClient(matching_app) as matching_http:
                    transport = MatchingTransport(matching_http)
                    directory = StaticIdentityDirectory(
                        {
                            "owner": ("owner@example.com", True),
                            "other": ("other@example.com", False),
                        }
                    )
                    sender = CaptureSender()
                    app = create_app(
                        NotificationSettings(
                            _env_file=None,
                            user_client_ids=["web"],
                            operator_client_ids=["ops"],
                        ),
                        token_verifier=Verifier(),
                        database=dbs["notification_api"],
                        admin_database=dbs["notification_admin"],
                    )
                    worker = NotificationWorker(
                        dbs["notification_worker"],
                        matching_client=MatchingClient(
                            "https://matching.invalid", lambda: "notify", transport=transport
                        ),
                        directory=directory,
                        sender=sender,
                        owner_app_base_url=OWNER_APP,
                    )
                    with TestClient(app) as http:
                        yield SimpleNamespace(
                            client=http,
                            item=item_http,
                            matching=matching_http,
                            transport=transport,
                            directory=directory,
                            sender=sender,
                            worker=worker,
                            matching_worker=MatchingWorker(
                                dbs["matching_worker"],
                                settings=matching_settings,
                                encoder=encoder,
                                item_client=item_client,
                                storage=storage,
                            ),
                            item_worker=ItemWorker(dbs["item_worker"], storage=storage),
                            db=dbs,
                            roles=roles,
                            tenants=tenants,
                            configs=configs,
                            migration_url=migration_url,
                        )
        finally:
            for db in dbs.values():
                db.close()
            if database_created:
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
            for role in created:
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
