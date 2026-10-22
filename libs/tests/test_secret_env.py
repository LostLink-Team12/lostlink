import json
from urllib.parse import parse_qs, urlsplit

import pytest
from lostlink_common.secret_env import (
    MAPPING_ENV,
    SecretConfigError,
    main,
    parse_mapping,
    prepare_environment,
)

DATABASE = {
    "username": "lostlink_item_worker",
    "password": "p@ss/word:%?&",
    "host": "db.internal",
    "port": 5432,
    "dbname": "lostlink",
}
MACHINE = {
    "client_id": "abc",
    "client_secret": "s3cret",
    "token_url": "https://auth.example.com/oauth2/token",
    "scopes": ["lostlink/items.index"],
}
SECRETS = {"db/item/worker": DATABASE, "machine/item": MACHINE}


def fetcher(calls):
    def fetch(secret):
        calls.append(secret)
        return json.dumps(SECRETS[secret])

    return fetch


def mapping(**entries):
    return {MAPPING_ENV: json.dumps(entries)}


def test_database_url_escapes_credentials_and_requires_verified_tls():
    calls = []
    env = prepare_environment(
        {
            "SERVICE_NAME": "item-service",
            "DATABASE_SSLROOTCERT": "/ca/rds.pem",
            **mapping(WORKER_DATABASE_URL={"secret": "db/item/worker", "format": "database"}),
        },
        fetcher(calls),
    )
    url = urlsplit(env["WORKER_DATABASE_URL"])
    assert url.scheme == "postgresql+psycopg"
    assert url.username == "lostlink_item_worker"
    assert url.hostname == "db.internal" and url.port == 5432
    assert "p%40ss%2Fword%3A%25%3F%26" in env["WORKER_DATABASE_URL"]
    query = parse_qs(url.query)
    assert query["sslmode"] == ["verify-full"] and query["sslrootcert"] == ["/ca/rds.pem"]
    assert env["SERVICE_NAME"] == "item-service" and MAPPING_ENV not in env


def test_each_secret_is_fetched_once_and_fields_are_copied():
    calls = []
    env = prepare_environment(
        mapping(
            MACHINE_CLIENT_ID={"secret": "machine/item", "key": "client_id"},
            MACHINE_CLIENT_SECRET={"secret": "machine/item", "key": "client_secret"},
            MACHINE_SCOPES={"secret": "machine/item", "key": "scopes"},
        ),
        fetcher(calls),
    )
    assert calls == ["machine/item"]
    assert env["MACHINE_CLIENT_SECRET"] == "s3cret"
    assert json.loads(env["MACHINE_SCOPES"]) == ["lostlink/items.index"]


def test_without_mapping_the_environment_is_unchanged():
    assert prepare_environment({"A": "1"}, lambda _: pytest.fail("no fetch")) == {"A": "1"}


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "not json",
        json.dumps({"lower": {"secret": "s", "key": "k"}}),
        json.dumps({"X": {"key": "k"}}),
        json.dumps({"X": {"secret": "s"}}),
        json.dumps({"X": {"secret": "s", "key": "k", "format": "database"}}),
        json.dumps({"X": {"secret": "s", "format": "yaml"}}),
    ],
)
def test_invalid_mappings_are_rejected(raw):
    with pytest.raises(SecretConfigError):
        parse_mapping(raw)


def test_explicit_values_cannot_be_shadowed():
    environ = {"MACHINE_CLIENT_ID": "x", **mapping(MACHINE_CLIENT_ID={"secret": "s", "key": "k"})}
    with pytest.raises(SecretConfigError, match="MACHINE_CLIENT_ID"):
        prepare_environment(environ, lambda _: "{}")


def test_errors_name_fields_but_never_values():
    secret = {"username": "u", "password": "hunter2-value"}
    with pytest.raises(SecretConfigError) as caught:
        prepare_environment(
            mapping(DATABASE_URL={"secret": "db", "format": "database"}),
            lambda _: json.dumps(secret),
        )
    assert "host" in str(caught.value) and "hunter2" not in str(caught.value)
    with pytest.raises(SecretConfigError, match="no field client_secret"):
        prepare_environment(
            mapping(MACHINE_CLIENT_SECRET={"secret": "m", "key": "client_secret"}),
            lambda _: json.dumps({"client_id": "abc"}),
        )


def test_main_requires_a_command():
    with pytest.raises(SystemExit):
        main(["--"])
