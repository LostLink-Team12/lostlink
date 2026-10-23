"""In-cluster DDL bootstrap. Administrator input is a temporary Secret, not SQL text."""

import json
import os
import sys

import psycopg
from psycopg import sql


def connect(value, **options):
    return psycopg.connect(
        host=value["host"],
        port=value["port"],
        dbname=value["dbname"],
        user=value["username"],
        password=value["password"],
        sslmode="verify-full",
        sslrootcert="/bootstrap/global-bundle.pem",
        connect_timeout=10,
        **options,
    )


def initialize(payload):
    with connect(payload["master"], autocommit=True) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        for key, value in payload["roles"].items():
            name = value["username"]
            if not connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)
            ).fetchone():
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOINHERIT NOBYPASSRLS"
                    ).format(sql.Identifier(name))
                )
            flags = connection.execute(
                "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s", (name,)
            ).fetchone()
            if any(flags):
                raise RuntimeError("Refusing to reuse an administrative runtime or migration role")
            connection.execute(
                sql.SQL(
                    "ALTER ROLE {} WITH LOGIN NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}"
                ).format(sql.Identifier(name), sql.Literal(value["password"]))
            )
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(value["dbname"]), sql.Identifier(name)
                )
            )
            if key.endswith("/migration"):
                connection.execute(
                    sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                        sql.Identifier(value["dbname"]), sql.Identifier(name)
                    )
                )
                service = key.split("/")[0]
                owner = connection.execute(
                    "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=%s",
                    (service,),
                ).fetchone()
                if owner and owner[0] != name:
                    raise RuntimeError("Existing schema owner differs from the intended migrator")
                # RDS administrator needs temporary membership to create a schema
                # owned by another non-superuser. Remove it immediately afterwards.
                admin = payload["master"]["username"]
                connection.execute(
                    sql.SQL("GRANT {} TO {}").format(sql.Identifier(name), sql.Identifier(admin))
                )
                try:
                    connection.execute(
                        sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(
                            sql.Identifier(service), sql.Identifier(name)
                        )
                    )
                finally:
                    connection.execute(
                        sql.SQL("REVOKE {} FROM {}").format(
                            sql.Identifier(name), sql.Identifier(admin)
                        )
                    )
        print("Initialized independent runtime and schema-owner migration roles; TLS verified.")


def finalize(payload):
    with connect(payload["master"], autocommit=True) as connection:
        for key, value in payload["roles"].items():
            if (
                key.endswith("/migration")
                and connection.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname=%s", (value["username"],)
                ).fetchone()
            ):
                connection.execute(
                    sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(
                        sql.Identifier(value["dbname"]), sql.Identifier(value["username"])
                    )
                )
    print("Revoked temporary database CREATE permission from all migration roles.")


def verify(payload):
    checked = 0
    for key, value in payload["roles"].items():
        if key.endswith("/migration"):
            continue
        service = key.split("/")[0]
        with connect(value) as connection:
            flags = connection.execute(
                "SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole "
                "FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            if any(flags):
                raise RuntimeError("Runtime role has administrative privileges")
            owned = connection.execute(
                "SELECT count(*) FROM pg_class "
                "WHERE relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)"
            ).fetchone()[0]
            if (
                owned
                or connection.execute(
                    "SELECT has_schema_privilege(current_user,%s,'CREATE')", (service,)
                ).fetchone()[0]
            ):
                raise RuntimeError("Runtime identity can own or create application tables")
            for other in ["venue", "item", "matching", "claim", "notification"]:
                if (
                    other != service
                    and connection.execute(
                        "SELECT has_schema_privilege(current_user,%s,'USAGE')", (other,)
                    ).fetchone()[0]
                ):
                    raise RuntimeError("Runtime identity can access another service schema")
            connection.execute(
                sql.SQL("SELECT version_num FROM {}.alembic_version").format(
                    sql.Identifier(service)
                )
            ).fetchone()
            checked += 1
    with connect(payload["master"]) as connection:
        unsafe = connection.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=ANY(%s) AND c.relkind='r' AND c.relname!='alembic_version' "
            "AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity)",
            (["venue", "item", "matching", "claim", "notification"],),
        ).fetchone()[0]
        if unsafe:
            raise RuntimeError("Some application tables do not enforce row-level security")
    print(
        f"Verified {checked} runtime roles: TLS, no bypass/DDL/ownership, "
        "isolated schemas, migrations and forced RLS."
    )


def main():
    try:
        payload = json.loads(os.environ["BOOTSTRAP_CREDENTIALS"])
        {"initialize": initialize, "verify": verify, "finalize": finalize}[sys.argv[1]](payload)
    except Exception as error:
        # SQL errors can contain password-bearing DDL. Report only the exception
        # class; the operator can diagnose metadata without exposing credentials.
        print(f"Database bootstrap failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
