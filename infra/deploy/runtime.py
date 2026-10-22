"""Fetch only this process's IRSA-authorized secrets, then replace PID 1.

No credential values are written to disk, command arguments or logs.
"""

import json
import os
import sys
from urllib.parse import quote

import boto3
from botocore.config import Config


def database_url(value):
    return (
        f"postgresql+psycopg://{quote(value['username'], safe='')}:"
        f"{quote(value['password'], safe='')}@{value['host']}:{value['port']}/{value['dbname']}"
        "?sslmode=verify-full&sslrootcert=/trust/rds.pem&connect_timeout=10"
    )


def main():
    client = boto3.client(
        "secretsmanager",
        config=Config(
            connect_timeout=5,
            read_timeout=15,
            retries={"mode": "standard", "total_max_attempts": 3},
        ),
    )
    try:
        for name, arn in json.loads(os.environ.get("DATABASE_SECRET_ARNS", "{}")).items():
            value = json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
            os.environ[name] = database_url(value)
        if arn := os.environ.get("MACHINE_SECRET_ARN"):
            value = json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
            for key in ["client_id", "client_secret", "token_url"]:
                os.environ["MACHINE_" + key.upper()] = value[key]
        if arn := os.environ.get("CRYPTO_SECRET_ARN"):
            value = json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
            for key in [
                "FIELD_ENCRYPTION_KEYS",
                "FIELD_ENCRYPTION_ACTIVE_KEY_ID",
                "HANDOVER_TOKEN_KEY",
            ]:
                os.environ[key] = value[key]
    finally:
        client.close()
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
