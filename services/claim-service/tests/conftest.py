"""One random database, nine restricted roles and real Venue/Item/Claim applications."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from claim_app.clients import ItemClient, ItemUnavailable
from claim_app.config import ClaimSettings
from claim_app.main import create_app
from claim_app.workers import ClaimWorker
from fastapi.testclient import TestClient
from item_app.config import ItemSettings
from item_app.database import ItemDatabase
from item_app.main import create_app as create_item_app
from item_app.workers import ItemWorker
from lostlink_common.auth import InvalidToken, Principal
from lostlink_common.config import Settings
from lostlink_common.crypto import FieldCipher
from lostlink_common.db import Database
from lostlink_common.identity import StaticIdentityDirectory
from lostlink_common.membership import MembershipClient
from psycopg import sql
from sqlalchemy.engine import make_url
from venue_app.config import VenueSettings
from venue_app.database import VenueDatabase
from venue_app.main import create_app as create_venue_app

SERVICES = {
    "venue": ["api", "platform", "worker", "identity"],
    "item": ["api", "internal", "worker"],
    "claim": ["api", "worker"],
}


class Storage:
    def ready(self, *_):
        pass

    def presign(self, *_):
        return {"url": "https://test-storage.invalid", "fields": {}}

    def photo_url(self, *_):
        return "https://test-storage.invalid/read"

    def seal(self, *_):
        return '"sealed"'


class ItemTransport:
    """Routes Claim's HTTPS client into the real Item app; tests can inject outages."""

    def __init__(self, client):
        self.client, self.failures, self.calls = client, [], []

    def __call__(self, method, url, headers, body=None):
        path = urlsplit(url).path
        self.calls.append((method, path))
        failure = self.failures.pop(0) if self.failures else None
        if failure == "before":
            raise ItemUnavailable("injected outage before the request")
        response = self.client.request(method, path, headers=headers, json=body)
        if failure == "lost":
            raise ItemUnavailable("injected lost response after Item committed")
        return response.status_code, response.json() if response.content else None


@pytest.fixture
def claim_env():
    admin_url = os.getenv("LOSTLINK_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set LOSTLINK_TEST_ADMIN_URL to a local PostgreSQL administrator URL")
    suffix, password = uuid4().hex, uuid4().hex
    base = make_url(admin_url)
    name = f"ll_claim_test_{suffix}"
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
            classes = {"venue": VenueDatabase, "item": ItemDatabase, "claim": Database}
            for key, role in roles.items():
                url = base.set(database=name, username=role, password=password)
                dbs[key] = classes[key.split("_")[0]](
                    Settings(
                        _env_file=None,
                        database_url=url.render_as_string(hide_password=False),
                        db_pool_size=4,
                    )
                )
            identities = {
                "ops": Principal(subject="operator", client_id="ops", roles={"platform_admin"}),
                "item-check": Principal(
                    subject="item-service",
                    client_id="item-machine",
                    scopes={"lostlink/members.check"},
                ),
                "claim-check": Principal(
                    subject="claim-service",
                    client_id="claim",
                    scopes={"lostlink/members.check"},
                ),
                "claim": Principal(
                    subject="claim-service",
                    client_id="claim",
                    scopes={"lostlink/items.context", "lostlink/items.reserve"},
                ),
                "index": Principal(
                    subject="matching-service", client_id="index", scopes={"lostlink/items.index"}
                ),
                "owner": Principal(subject="owner-1", client_id="web", roles={"owner"}),
                "owner2": Principal(subject="owner-2", client_id="web", roles={"owner"}),
                "unverified": Principal(subject="owner-3", client_id="web", roles={"owner"}),
            }

            class Verifier:
                def verify(self, token):
                    if token not in identities:
                        raise InvalidToken()
                    return identities[token]

            venue_app = create_venue_app(
                VenueSettings(
                    _env_file=None,
                    user_client_ids=["web"],
                    platform_client_ids=["ops"],
                    member_check_client_ids=["item-machine", "claim"],
                ),
                token_verifier=Verifier(),
                database=dbs["venue_api"],
                platform_database=dbs["venue_platform"],
            )
            with TestClient(venue_app) as venue_http:
                tenants = {}
                for key in ["a", "b"]:
                    response = venue_http.post(
                        "/v1/admin/venues",
                        headers={"Authorization": "Bearer ops", "Idempotency-Key": uuid4().hex},
                        json={
                            "name": f"Venue {key}",
                            "slug": key,
                            "initial_admin_sub": f"admin-{key}",
                        },
                    )
                    assert response.status_code == 201, response.text
                    tenants[key] = UUID(response.json()["venue"]["id"])
                    # Every Cognito user also carries the owner group (UC1 token trigger).
                    identities[key] = Principal(
                        subject=f"admin-{key}",
                        client_id="web",
                        tenant_id=tenants[key],
                        roles={"venue_admin", "owner"},
                    )

                def membership(token):
                    def transport(_url, headers, body):
                        response = venue_http.post(
                            "/internal/v1/members/check", headers=headers, json=json.loads(body)
                        )
                        assert response.status_code == 200, response.text
                        return response.json()

                    return MembershipClient(
                        "https://venue.invalid", lambda: token, transport=transport
                    )

                storage = Storage()
                item_app = create_item_app(
                    ItemSettings(
                        _env_file=None,
                        environment="test",
                        user_client_ids=["web"],
                        claim_client_ids=["claim"],
                        index_client_ids=["index"],
                        photo_bucket="test-photos",
                    ),
                    token_verifier=Verifier(),
                    database=dbs["item_api"],
                    internal_database=dbs["item_internal"],
                    membership_client=membership("item-check"),
                    storage=storage,
                )
                with TestClient(item_app) as item_http:
                    transport = ItemTransport(item_http)
                    item_client = ItemClient(
                        "https://item.invalid", lambda: "claim", transport=transport
                    )
                    cipher = FieldCipher({"k1": os.urandom(32)}, "k1")
                    directory = StaticIdentityDirectory(
                        {
                            "owner-1": ("owner1@example.com", True),
                            "owner-2": ("owner2@example.com", True),
                            "owner-3": ("owner3@example.com", False),
                            "admin-a": ("admin-a@example.com", True),
                        }
                    )
                    app = create_app(
                        ClaimSettings(_env_file=None, user_client_ids=["web"]),
                        token_verifier=Verifier(),
                        database=dbs["claim_api"],
                        membership_client=membership("claim-check"),
                        item_client=item_client,
                        identity_directory=directory,
                        cipher=cipher,
                        token_key=os.urandom(32),
                    )
                    with TestClient(app) as http:
                        yield SimpleNamespace(
                            client=http,
                            app=app,
                            item=item_http,
                            venue=venue_http,
                            transport=transport,
                            item_client=item_client,
                            identities=identities,
                            tenants=tenants,
                            db=dbs,
                            roles=roles,
                            configs=configs,
                            migration_url=migration_url,
                            directory=directory,
                            worker=ClaimWorker(dbs["claim_worker"], item_client=item_client),
                            item_worker=ItemWorker(dbs["item_worker"], storage=storage),
                        )
        finally:
            for db in dbs.values():
                db.close()
            if database_created:
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
            for role in created:
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
