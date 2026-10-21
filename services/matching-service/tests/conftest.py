"""Disposable PostgreSQL roles and real Item/Matching APIs with deterministic inference."""

import io
import os
from pathlib import Path
from types import SimpleNamespace
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
from matching_app.clients import ItemClient
from matching_app.config import MatchingSettings
from matching_app.database import MatchingDatabase
from matching_app.embedding import EmbeddingError
from matching_app.main import create_app
from matching_app.workers import MatchingWorker
from psycopg import sql
from sqlalchemy.engine import make_url


class TestEncoder:
    __test__ = False
    model_version = "test-clip-512"

    def ready(self):
        pass

    def encode(self, text=None, photo=None):
        if photo == b"invalid":
            raise EmbeddingError("invalid_image", False)
        value = [0.0] * 512
        value[0 if text and "red" in text.lower() else 1] = 1.0
        return value


@pytest.fixture
def matching_env():
    admin_url = os.getenv("LOSTLINK_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set LOSTLINK_TEST_ADMIN_URL")
    suffix, password = uuid4().hex, uuid4().hex
    base = make_url(admin_url)
    name = f"ll_matching_test_{suffix}"
    roles = {
        f"{svc}_{mode}": f"ll_{svc[0]}_{mode}_{suffix}"
        for svc in ["matching", "item"]
        for mode in ["api", "internal", "worker"]
    }
    dbs, created, database_created = {}, [], False
    with psycopg.connect(
        base.set(drivername="postgresql").render_as_string(hide_password=False), autocommit=True
    ) as admin:
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
            configs = {}
            root = Path(__file__).parents[3]
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("MIGRATION_DATABASE_URL", migration_url)
                for key, value in roles.items():
                    patch.setenv(key.upper() + "_ROLE", value)
                for svc in ["item", "matching"]:
                    config = Config(str(root / f"services/{svc}-service/alembic.ini"))
                    configs[svc] = config
                    command.upgrade(config, "head")
                    command.upgrade(config, "head")
            for key, role in roles.items():
                cls = MatchingDatabase if key.startswith("matching") else ItemDatabase
                dbs[key] = cls(
                    Settings(
                        _env_file=None,
                        database_url=base.set(
                            database=name, username=role, password=password
                        ).render_as_string(hide_password=False),
                    )
                )
            tenants = {"a": uuid4(), "b": uuid4()}
            identities = {
                "owner": Principal(subject="owner", client_id="web", roles={"owner"}),
                "other": Principal(subject="other", client_id="web", roles={"owner"}),
                "index": Principal(
                    subject="matching", client_id="index", scopes={"lostlink/items.index"}
                ),
                "notify": Principal(
                    subject="notification", client_id="notify", scopes={"lostlink/matches.notify"}
                ),
                "wrong-scope": Principal(subject="notification", client_id="notify"),
            }
            for key, tenant in tenants.items():
                identities[key] = Principal(
                    subject=f"staff-{key}", client_id="web", tenant_id=tenant, roles={"venue_staff"}
                )

            class Verifier:
                def verify(self, token):
                    if token not in identities:
                        raise InvalidToken()
                    return identities[token]

            class Membership:
                def require_current(self, *args):
                    pass

            class Storage:
                content = b"valid test photo"

                def ready(self, *args):
                    pass

                def presign(self, *args):
                    return {"url": "https://storage.invalid", "fields": {}}

                def seal(self, *args):
                    return '"sealed"'

                def photo_url(self, *args):
                    return "https://storage.invalid/signed-photo"

                def get_object(self, **kwargs):
                    return {"Body": io.BytesIO(self.content), "ContentLength": len(self.content)}

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

                def transport(url, headers):
                    response = item_http.get(urlsplit_path(url), headers=headers)
                    if response.status_code == 404:
                        return None
                    assert response.status_code == 200, response.text
                    return response.json()

                item_client = ItemClient(
                    "https://item.invalid", lambda: "index", transport=transport
                )
                settings = MatchingSettings(
                    _env_file=None,
                    user_client_ids=["web"],
                    notification_client_ids=["notify"],
                    item_photo_buckets=["test-photos"],
                    match_threshold=0.5,
                )
                encoder = TestEncoder()
                app = create_app(
                    settings,
                    database=dbs["matching_api"],
                    internal_database=dbs["matching_internal"],
                    token_verifier=Verifier(),
                    encoder=encoder,
                    item_client=item_client,
                )
                worker = MatchingWorker(
                    dbs["matching_worker"],
                    settings=settings,
                    encoder=encoder,
                    item_client=item_client,
                    storage=storage,
                )
                with TestClient(app) as http:
                    yield SimpleNamespace(
                        client=http,
                        app=app,
                        item=item_http,
                        db=dbs,
                        roles=roles,
                        tenants=tenants,
                        identities=identities,
                        encoder=encoder,
                        worker=worker,
                        item_worker=ItemWorker(dbs["item_worker"], storage=storage),
                        item_client=item_client,
                        storage=storage,
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


def urlsplit_path(url):
    from urllib.parse import urlsplit

    return urlsplit(url).path
