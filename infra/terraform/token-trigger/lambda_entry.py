"""Load the identity credential, then delegate all claim rules to venue_app."""

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import boto3
from botocore.config import Config

_secrets = boto3.Session().client(
    "secretsmanager",
    config=Config(
        connect_timeout=1, read_timeout=1, retries={"mode": "standard", "total_max_attempts": 1}
    ),
)
_expires = 0.0


def handler(event, context):
    global _expires
    if event.get("triggerSource") == "TokenGeneration_ClientCredentials":
        return event
    if time.monotonic() >= _expires:
        value = json.loads(
            _secrets.get_secret_value(SecretId=os.environ["IDENTITY_SECRET_ARN"])["SecretString"]
        )
        url = (
            f"postgresql+psycopg://{quote(value['username'], safe='')}:"
            f"{quote(value['password'], safe='')}@{value['host']}:{value['port']}/{value['dbname']}"
            f"?sslmode=verify-full&sslrootcert={Path(__file__).with_name('global-bundle.pem')}"
        )
        if os.environ.get("IDENTITY_DATABASE_URL") != url:
            os.environ["IDENTITY_DATABASE_URL"] = url
            from venue_app.token_trigger import dependencies

            if dependencies.cache_info().currsize:
                dependencies()[1].engine.dispose()
            dependencies.cache_clear()
        _expires = time.monotonic() + 60
    from venue_app.token_trigger import handler as venue_handler

    return venue_handler(event, context)
