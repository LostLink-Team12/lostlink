from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from lostlink_common.auth import InvalidToken, Principal
from lostlink_common.config import Settings
from lostlink_common.db import Database, get_owner_session
from lostlink_common.errors import install_error_handlers


@pytest.fixture
def database():
    # Engines connect lazily; these tests fail before any connection is opened.
    database = Database(
        Settings(_env_file=None, database_url="postgresql+psycopg://u:p@127.0.0.1:9/none")
    )
    yield database
    database.close()


@pytest.mark.parametrize(
    "context",
    [
        {"subject": "x" * 129},
        {"subject": "owner\x00"},
        {"tenant_id": "not-a-uuid"},
        {"resource_id": 7},
    ],
)
def test_scoped_session_rejects_untrusted_context_values(database, context):
    with pytest.raises(ValueError):
        with database.scoped_session(**context):
            pass


def test_actor_sessions_require_explicit_mode_and_verified_tenant(database):
    owner = Principal(subject="owner", client_id="web", roles={"owner"})
    with pytest.raises(ValueError, match="tenant"):
        with database.actor_session(owner, venue_scope=True):
            pass
    with pytest.raises(TypeError):
        database.actor_session(owner)
    with pytest.raises(ValueError):
        with database.actor_session(
            Principal(subject="x" * 129, client_id="web"), venue_scope=False
        ):
            pass


class Verifier:
    tokens = {
        "owner": Principal(subject="owner-1", client_id="web", roles={"owner"}),
        "machine": Principal(subject="client", client_id="m2m", scopes={"lostlink/x"}),
        "staff": Principal(
            subject="staff-1", client_id="web", tenant_id=uuid4(), roles={"venue_staff"}
        ),
    }

    def verify(self, token):
        if token not in self.tokens:
            raise InvalidToken()
        return self.tokens[token]


class RecordingDatabase:
    def __init__(self):
        self.calls = []

    def actor_session(self, principal, *, venue_scope):
        self.calls.append((principal.subject, principal.tenant_id, venue_scope))
        return self

    def __enter__(self):
        return "session"

    def __exit__(self, *_):
        return False


def owner_app(database):
    app = FastAPI()
    install_error_handlers(app)
    app.state.token_verifier, app.state.database = Verifier(), database

    @app.get("/mine")
    def mine(session: Annotated[object, Depends(get_owner_session)]):
        return {"session": session}

    return app


def test_owner_session_dependency_rejects_machine_and_non_owner_tokens():
    database = RecordingDatabase()
    client = TestClient(owner_app(database))
    assert client.get("/mine").status_code == 401
    assert client.get("/mine", headers={"Authorization": "Bearer bad"}).status_code == 401
    for token in ["machine", "staff"]:
        response = client.get("/mine", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403
    response = client.get("/mine", headers={"Authorization": "Bearer owner"})
    assert response.status_code == 200
    assert database.calls == [("owner-1", None, False)]


def test_owner_session_dependency_requires_a_configured_database():
    client = TestClient(owner_app(None))
    response = client.get("/mine", headers={"Authorization": "Bearer owner"})
    assert response.status_code == 503
