"""Each integration case owns one random database and seven restricted service roles."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from item_app.config import ItemSettings
from item_app.database import ItemDatabase
from item_app.main import create_app
from item_app.storage import PhotoStorage, s3_client
from item_app.workers import ItemWorker
from lostlink_common.auth import InvalidToken, Principal
from lostlink_common.config import Settings
from lostlink_common.membership import MembershipClient
from psycopg import sql
from sqlalchemy.engine import make_url
from venue_app.config import VenueSettings
from venue_app.database import VenueDatabase
from venue_app.main import create_app as create_venue_app


@pytest.fixture
def item_env():
    admin_url = os.getenv("LOSTLINK_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set LOSTLINK_TEST_ADMIN_URL to a local PostgreSQL administrator URL")
    suffix = uuid4().hex
    base = make_url(admin_url)
    database_name = f"ll_item_test_{suffix}"
    roles = {
        f"{service}_{mode}": f"ll_{service[0]}_{mode}_{suffix}"
        for service, modes in [
            ("item", ["api", "internal", "worker"]),
            ("venue", ["api", "platform", "worker", "identity"]),
        ]
        for mode in modes
    }
    password, connections, created_roles = uuid4().hex, {}, []
    database_created = False
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
                created_roles.append(role)
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
            database_created = True
            migration_url = base.set(database=database_name).render_as_string(hide_password=False)
            root = Path(__file__).parents[3]
            configs = {}
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("MIGRATION_DATABASE_URL", migration_url)
                for name, role in roles.items():
                    patch.setenv(name.upper() + "_ROLE", role)
                for service in ["venue", "item"]:
                    config = Config(str(root / f"services/{service}-service/alembic.ini"))
                    configs[service] = config
                    command.upgrade(config, "head")
                    command.upgrade(config, "head")
            for name, role in roles.items():
                url = base.set(database=database_name, username=role, password=password)
                cls = VenueDatabase if name.startswith("venue") else ItemDatabase
                connections[name] = cls(
                    Settings(
                        _env_file=None,
                        database_url=url.render_as_string(hide_password=False),
                        db_pool_size=4,
                    )
                )
            identities = {
                "ops": Principal(subject="operator", client_id="ops", roles={"platform_admin"}),
                "venue-check": Principal(
                    subject="item-service",
                    client_id="item-machine",
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
                "owner": Principal(subject="owner", client_id="web", roles={"owner"}),
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
                    member_check_client_ids=["item-machine"],
                ),
                token_verifier=Verifier(),
                database=connections["venue_api"],
                platform_database=connections["venue_platform"],
            )

            class Storage:
                def ready(self, _):
                    pass

                def presign(self, _):
                    return {"url": "https://test-storage.invalid", "fields": {}}

                def photo_url(self, _):
                    return "https://test-storage.invalid/read"

                def seal(self, _):
                    return '"sealed"'

            storage = Storage()
            with TestClient(venue_app) as venue_client:
                tenants, admins = {}, {}
                for name in ["a", "b"]:
                    response = venue_client.post(
                        "/v1/admin/venues",
                        headers={"Authorization": "Bearer ops", "Idempotency-Key": uuid4().hex},
                        json={
                            "name": f"Venue {name}",
                            "slug": name,
                            "initial_admin_sub": f"admin-{name}",
                        },
                    )
                    assert response.status_code == 201, response.text
                    tenants[name] = UUID(response.json()["venue"]["id"])
                    admins[name] = response.json()["initial_admin"]["id"]
                    identities[name] = Principal(
                        subject=f"admin-{name}",
                        client_id="web",
                        tenant_id=tenants[name],
                        roles={"venue_admin"},
                    )

                def transport(_url, headers, body):
                    response = venue_client.post(
                        "/internal/v1/members/check", headers=headers, json=json.loads(body)
                    )
                    assert response.status_code == 200, response.text
                    return response.json()

                membership = MembershipClient(
                    "https://venue.invalid", lambda: "venue-check", transport=transport
                )
                settings = ItemSettings(
                    _env_file=None,
                    environment="test",
                    user_client_ids=["web"],
                    claim_client_ids=["claim"],
                    index_client_ids=["index"],
                    photo_bucket="test-photos",
                )
                app = create_app(
                    settings,
                    token_verifier=Verifier(),
                    database=connections["item_api"],
                    internal_database=connections["item_internal"],
                    membership_client=membership,
                    storage=storage,
                )
                with TestClient(app) as client:
                    yield SimpleNamespace(
                        client=client,
                        app=app,
                        venue=venue_client,
                        identities=identities,
                        tenants=tenants,
                        admins=admins,
                        db=connections,
                        roles=roles,
                        configs=configs,
                        migration_url=migration_url,
                        storage=storage,
                        worker=ItemWorker(connections["item_worker"], storage=storage),
                    )
        finally:
            for connection in connections.values():
                connection.close()
            if database_created:
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))
            for role in created_roles:
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.fixture
def item_s3(item_env):
    endpoint = os.getenv("LOSTLINK_TEST_AWS_ENDPOINT")
    if not endpoint:
        pytest.skip("Set LOSTLINK_TEST_AWS_ENDPOINT to a LocalStack endpoint")
    client = s3_client(ItemSettings(_env_file=None, aws_endpoint_url=endpoint))
    bucket = f"item-test-{uuid4().hex}"
    client.create_bucket(
        Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-1"}
    )
    storage = PhotoStorage(client)
    item_env.app.state.storage = storage
    item_env.app.state.settings.photo_bucket = bucket
    item_env.worker.storage = storage
    try:
        yield client, bucket
    finally:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            keys = [{"Key": value["Key"]} for value in page.get("Contents", [])]
            if keys:
                client.delete_objects(Bucket=bucket, Delete={"Objects": keys})
        client.delete_bucket(Bucket=bucket)
        client.close()
