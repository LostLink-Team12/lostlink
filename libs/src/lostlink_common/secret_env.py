"""Load per-process credentials from AWS Secrets Manager into memory, then exec the process.

Kubernetes pods run ``python -m lostlink_common.secret_env -- <command>``. The pod's own
IRSA role can read only its own secrets, so nothing is copied into Kubernetes Secrets,
files or Terraform state. ``LOSTLINK_SECRET_ENV`` maps environment names to secrets:

    {"WORKER_DATABASE_URL": {"secret": "lostlink-dev/database/item/worker", "format": "database"},
     "MACHINE_CLIENT_SECRET": {"secret": "lostlink-dev/machine/item", "key": "client_secret"}}

``format: database`` builds a ``postgresql+psycopg`` URL from the JSON fields ``username``,
``password``, ``host``, ``port`` and ``dbname`` with ``sslmode=verify-full`` and the RDS CA
bundle in ``DATABASE_SSLROOTCERT``. ``key`` copies one JSON field; non-string values are
re-encoded as JSON text. Values never appear in logs or error messages.
"""

import json
import os
import sys
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

MAPPING_ENV = "LOSTLINK_SECRET_ENV"
DEFAULT_CA_BUNDLE = "/etc/lostlink/rds/global-bundle.pem"
DATABASE_FIELDS = ("username", "password", "host", "port", "dbname")


class SecretConfigError(RuntimeError):
    """Raised with the variable or secret name only, never a secret value."""


def parse_mapping(raw: str) -> dict[str, dict[str, str]]:
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SecretConfigError(f"{MAPPING_ENV} must be a JSON object") from exc
    if not isinstance(mapping, dict):
        raise SecretConfigError(f"{MAPPING_ENV} must be a JSON object")
    for name, spec in mapping.items():
        if not name or not name.replace("_", "").isalnum() or not name.isupper():
            raise SecretConfigError(f"Invalid environment name {name!r}")
        if not isinstance(spec, dict) or not isinstance(spec.get("secret"), str):
            raise SecretConfigError(f"{name} needs a secret name")
        if ("key" in spec) == (spec.get("format") == "database"):
            raise SecretConfigError(f"{name} needs exactly one of key or format=database")
        if "format" in spec and spec["format"] != "database":
            raise SecretConfigError(f"{name} has an unknown format")
    return mapping


def database_url(value: Mapping[str, Any], ca_bundle: str) -> str:
    missing = [field for field in DATABASE_FIELDS if field not in value]
    if missing:
        raise SecretConfigError(f"Database secret is missing {', '.join(missing)}")
    return (
        f"postgresql+psycopg://{quote(str(value['username']), safe='')}:"
        f"{quote(str(value['password']), safe='')}@{value['host']}:{int(value['port'])}/"
        f"{quote(str(value['dbname']), safe='')}"
        f"?sslmode=verify-full&sslrootcert={quote(ca_bundle, safe='/')}&connect_timeout=10"
    )


def resolve(
    mapping: Mapping[str, Mapping[str, str]],
    fetch: Callable[[str], str],
    ca_bundle: str = DEFAULT_CA_BUNDLE,
) -> dict[str, str]:
    """Return environment values, fetching each distinct secret once."""
    cache: dict[str, dict[str, Any]] = {}
    resolved = {}
    for name, spec in mapping.items():
        secret = spec["secret"]
        if secret not in cache:
            try:
                value = json.loads(fetch(secret))
            except json.JSONDecodeError as exc:
                raise SecretConfigError(f"Secret {secret} is not a JSON object") from exc
            if not isinstance(value, dict):
                raise SecretConfigError(f"Secret {secret} is not a JSON object")
            cache[secret] = value
        value = cache[secret]
        if spec.get("format") == "database":
            resolved[name] = database_url(value, ca_bundle)
            continue
        if spec["key"] not in value:
            raise SecretConfigError(f"Secret {secret} has no field {spec['key']}")
        field = value[spec["key"]]
        resolved[name] = field if isinstance(field, str) else json.dumps(field)
    return resolved


def secrets_manager_fetcher(region: str | None) -> Callable[[str], str]:
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "secretsmanager",
        region_name=region,
        config=Config(connect_timeout=3, read_timeout=5, retries={"mode": "standard"}),
    )
    return lambda secret: client.get_secret_value(SecretId=secret)["SecretString"]


def prepare_environment(
    environ: Mapping[str, str], fetch: Callable[[str], str] | None = None
) -> dict[str, str]:
    raw = environ.get(MAPPING_ENV)
    if not raw:
        return dict(environ)
    mapping = parse_mapping(raw)
    clashes = sorted(set(mapping) & set(environ))
    if clashes:
        raise SecretConfigError(f"Already set outside Secrets Manager: {', '.join(clashes)}")
    fetch = fetch or secrets_manager_fetcher(environ.get("AWS_REGION"))
    ca_bundle = environ.get("DATABASE_SSLROOTCERT", DEFAULT_CA_BUNDLE)
    result = {key: value for key, value in environ.items() if key != MAPPING_ENV}
    result.update(resolve(mapping, fetch, ca_bundle))
    return result


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        raise SystemExit("usage: python -m lostlink_common.secret_env -- <command> [args...]")
    try:
        environment = prepare_environment(os.environ)
    except SecretConfigError as exc:
        raise SystemExit(f"secret_env: {exc}") from None
    os.execvpe(args[0], args, environment)


if __name__ == "__main__":
    main()
