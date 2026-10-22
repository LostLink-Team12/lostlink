"""Repeatable backend role bootstrap, Alembic upgrades and deployment checks.

Credentials come only from named environment variables. No command prints them.
Use `plan` first; `bootstrap` and `migrate` explicitly mutate the target database.
"""

import argparse
import base64
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libs/src"))
MODES = {
    "venue": ("api", "platform", "worker", "identity"),
    "item": ("api", "internal", "worker"),
    "matching": ("api", "internal", "worker"),
    "claim": ("api", "worker"),
    "notification": ("api", "admin", "worker"),
}
for service_name in MODES:
    sys.path.insert(0, str(ROOT / "services" / f"{service_name}-service"))

from lostlink_common.crypto import EncryptionContext, FieldCipher  # noqa: E402


class ToolError(Exception):
    """A fixed, non-sensitive diagnostic safe for command output."""


def require(condition, code):
    if not condition:
        raise ToolError(code)


class Role(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    password_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")


class Service(BaseModel):
    model_config = ConfigDict(extra="forbid")
    migration_url_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    roles: dict[str, Role]


class Deployment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    bootstrap_url_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    services: dict[str, Service]

    @model_validator(mode="after")
    def distinct_roles(self):
        require(set(self.services) == set(MODES), "configuration_services_invalid")
        for service, settings in self.services.items():
            require(set(settings.roles) == set(MODES[service]), "configuration_modes_invalid")
        roles = [role for service in self.services.values() for role in service.roles.values()]
        require(len({role.name for role in roles}) == len(roles), "runtime_roles_must_be_distinct")
        require(
            len({role.password_env for role in roles}) == len(roles),
            "password_variables_must_be_distinct",
        )
        url_variables = {self.bootstrap_url_env} | {
            service.migration_url_env for service in self.services.values()
        }
        require(
            not url_variables & {role.password_env for role in roles},
            "credential_variable_collision",
        )
        return self


def load(path):
    return Deployment.model_validate_json(Path(path).read_text(encoding="utf-8"))


def secret(name):
    value = os.environ.get(name)
    require(bool(value), f"missing_environment:{name}")
    return value


def database_url(variable, config, environment):
    url = make_url(secret(variable))
    require(url.drivername == "postgresql+psycopg", "database_driver_invalid")
    require(url.database == config.database, "database_target_mismatch")
    require(bool(url.host and url.username and url.password), "database_url_incomplete")
    if environment == "local":
        require(url.host in {"localhost", "127.0.0.1", "::1"}, "local_target_must_be_loopback")
    # Prevent connection-string parameters from overriding the validated target/login.
    require(
        set(url.query)
        <= {"sslmode", "sslrootcert", "sslcert", "sslkey", "channel_binding", "application_name"},
        "database_query_parameters_invalid",
    )
    if environment != "local":
        require(url.query.get("sslmode") == "verify-full", "database_tls_verify_full_required")
        certificate = url.query.get("sslrootcert")
        require(bool(certificate), "database_tls_ca_required")
        require(certificate == "system" or Path(certificate).is_file(), "database_tls_ca_not_found")
    return url


def connect(url):
    return psycopg.connect(
        host=url.host,
        port=url.port or 5432,
        dbname=url.database,
        user=url.username,
        password=url.password,
        **dict(url.query),
        connect_timeout=3,
        options="-c statement_timeout=10000 -c lock_timeout=5000",
    )


def prepared(config, services, environment):
    runtime_names = {r.name for s in config.services.values() for r in s.roles.values()}
    result = {}
    seen_passwords = set()
    for name in services:
        service = config.services[name]
        url = database_url(service.migration_url_env, config, environment)
        require(url.username not in runtime_names, "migration_identity_is_runtime_role")
        passwords = {}
        for mode, role in service.roles.items():
            value = secret(role.password_env)
            require(len(value) >= 16 and "\0" not in value, "database_password_invalid")
            require(
                value not in seen_passwords and value != url.password, "database_password_reused"
            )
            seen_passwords.add(value)
            passwords[mode] = value
        result[name] = (url, passwords)
    return result


def check_role(connection, role):
    row = connection.execute(
        "SELECT oid,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolinherit,rolreplication,"
        "rolbypassrls FROM pg_roles WHERE rolname=%s",
        (role.name,),
    ).fetchone()
    require(row is not None, f"role_missing:{role.name}")
    oid, login, *dangerous = row
    require(login and not any(dangerous), f"role_attributes_unsafe:{role.name}")
    require(
        not connection.execute("SELECT 1 FROM pg_auth_members WHERE member=%s", (oid,)).fetchone(),
        f"role_membership_unsafe:{role.name}",
    )
    require(
        not connection.execute(
            "SELECT 1 FROM pg_namespace WHERE nspowner=%s UNION ALL "
            "SELECT 1 FROM pg_class WHERE relowner=%s UNION ALL "
            "SELECT 1 FROM pg_database WHERE datdba=%s LIMIT 1",
            (oid, oid, oid),
        ).fetchone(),
        f"runtime_role_owns_objects:{role.name}",
    )


def authenticate(url, role, password):
    with connect(url.set(username=role.name, password=password)) as runtime:
        runtime.read_only = True
        require(
            runtime.execute("SELECT current_user").fetchone()[0] == role.name,
            "runtime_login_mismatch",
        )


def bootstrap(config, services, environment):
    inputs = prepared(config, services, environment)
    admin_url = database_url(config.bootstrap_url_env, config, environment)
    require(
        all(admin_url.password not in passwords.values() for _, passwords in inputs.values()),
        "database_password_reused",
    )
    require(
        all(
            (url.host, url.port or 5432) == (admin_url.host, admin_url.port or 5432)
            for url, _ in inputs.values()
        ),
        "bootstrap_server_mismatch",
    )
    created = 0
    with connect(admin_url) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtext('lostlink:bootstrap:roles'))")
        # Validate every existing role and its supplied password before any DDL.
        missing = []
        for name, (url, passwords) in inputs.items():
            for mode, role in config.services[name].roles.items():
                exists = connection.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname=%s",
                    (role.name,),
                ).fetchone()
                if exists:
                    check_role(connection, role)
                    authenticate(url, role, passwords[mode])
                else:
                    missing.append((role, passwords[mode]))
        if "matching" in services:
            connection.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            require(
                connection.execute(
                    "SELECT n.nspname FROM pg_extension e "
                    "JOIN pg_namespace n ON n.oid=e.extnamespace "
                    "WHERE e.extname='vector'",
                ).fetchone()
                == ("public",),
                "pgvector_schema_invalid",
            )
        for role, password in missing:
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(role.name), sql.Literal(password))
            )
            created += 1
        for name in services:
            for role in config.services[name].roles.values():
                connection.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(config.database),
                        sql.Identifier(role.name),
                    )
                )
    return {"created_roles": created, "existing_passwords": "preserved"}


def migrate(config, services, environment):
    inputs = prepared(config, services, environment)
    # Fail before running any service migration if credentials/role safety are wrong.
    for name, (url, passwords) in inputs.items():
        with connect(url) as connection:
            for mode, role in config.services[name].roles.items():
                check_role(connection, role)
                authenticate(url, role, passwords[mode])
    completed = []
    for name, (url, _) in inputs.items():
        with connect(url) as lock:
            lock.execute("SELECT pg_advisory_xact_lock(hashtext('lostlink:deployment:migration'))")
            # The migration child receives only its own DDL credential, not bootstrap,
            # other service passwords, AWS credentials or Claim encryption keys.
            host_variables = {
                "PATH",
                "SYSTEMROOT",
                "WINDIR",
                "HOME",
                "USERPROFILE",
                "TEMP",
                "TMP",
                "TMPDIR",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "PYTHONUTF8",
                "TZ",
            }
            child_environment = {
                key: value for key, value in os.environ.items() if key.upper() in host_variables
            }
            child_environment["PYTHONPATH"] = os.pathsep.join(
                [str(ROOT / "libs/src"), str(ROOT / f"services/{name}-service")]
            )
            child_environment["MIGRATION_DATABASE_URL"] = url.render_as_string(hide_password=False)
            for mode, role in config.services[name].roles.items():
                child_environment[f"{name}_{mode}_ROLE".upper()] = role.name
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "-c",
                    str(ROOT / f"services/{name}-service/alembic.ini"),
                    "upgrade",
                    "head",
                ],
                cwd=ROOT / f"services/{name}-service",
                env=child_environment,
                capture_output=True,
                text=True,
                timeout=180,
            )
            require(result.returncode == 0, f"migration_failed:{name}")
        completed.append(name)
    return {"upgraded": completed}


def check_schema(connection, name):
    models = importlib.import_module(f"{name}_app.models")
    configuration = Config(str(ROOT / f"services/{name}-service/alembic.ini"))
    head = ScriptDirectory.from_config(configuration).get_current_head()
    rows = connection.execute(
        sql.SQL("SELECT version_num FROM {}.alembic_version").format(
            sql.Identifier(name),
        )
    ).fetchall()
    require(rows == [(head,)], f"migration_revision_mismatch:{name}")
    for table in models.Base.metadata.sorted_tables:
        state = connection.execute(
            "SELECT c.relrowsecurity,c.relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s "
            "AND c.relkind IN ('r','p')",
            (name, table.name),
        ).fetchone()
        require(state == (True, True), f"table_or_forced_rls_invalid:{name}:{table.name}")
    public_acl = connection.execute(
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE n.nspname=%s AND a.grantee=0 "
        "UNION ALL SELECT 1 FROM pg_attribute t JOIN pg_class c ON c.oid=t.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(t.attacl) a "
        "WHERE n.nspname=%s AND a.grantee=0 LIMIT 1",
        (name, name),
    ).fetchone()
    require(not public_acl, f"public_data_privileges:{name}")


def check_permissions(connection, name, role):
    require(
        not connection.execute(
            "SELECT has_database_privilege(%s,current_database(),'CREATE')",
            (role.name,),
        ).fetchone()[0],
        f"runtime_can_create_schema:{role.name}",
    )
    require(
        not connection.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname=ANY(%s) "
            "AND has_schema_privilege(%s,oid,'CREATE') LIMIT 1",
            ([*MODES, "public"], role.name),
        ).fetchone(),
        f"runtime_can_create_objects:{role.name}",
    )
    require(
        connection.execute(
            "SELECT has_schema_privilege(%s,%s,'USAGE') AND has_table_privilege(%s,%s,'SELECT')",
            (role.name, name, role.name, f"{name}.alembic_version"),
        ).fetchone()[0],
        f"runtime_migration_read_missing:{role.name}",
    )
    require(
        not connection.execute(
            "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=ANY(%s) AND c.relkind IN ('r','p') AND ("
            "has_table_privilege(%s,c.oid,'TRUNCATE,TRIGGER,REFERENCES') OR "
            "has_any_column_privilege(%s,c.oid,'REFERENCES') OR "
            "(n.nspname<>%s AND (has_table_privilege(%s,c.oid,'SELECT,INSERT,UPDATE,DELETE') OR "
            "has_any_column_privilege(%s,c.oid,'SELECT,INSERT,UPDATE'))) OR "
            "(c.relname='alembic_version' AND "
            "(has_table_privilege(%s,c.oid,'INSERT,UPDATE,DELETE') OR "
            "has_any_column_privilege(%s,c.oid,'INSERT,UPDATE')))) LIMIT 1",
            (list(MODES), role.name, role.name, name, role.name, role.name, role.name, role.name),
        ).fetchone(),
        f"runtime_excess_privileges:{role.name}",
    )


# Selected privacy boundaries in addition to the generic separation checks above.
PRIVATE_COLUMNS = [
    ("venue", "api", "venues", "status", "UPDATE"),
    ("venue", "identity", "memberships", "role", "UPDATE"),
    ("item", "internal", "verification_questions", "reference_answer", "SELECT"),
    ("claim", "worker", "claim_contacts", "encrypted_contact", "SELECT"),
    ("claim", "api", "claims", "owner_sub", "UPDATE"),
    ("notification", "api", "notification_jobs", "state", "SELECT"),
    ("notification", "worker", "notifications", "owner_sub", "UPDATE"),
]
READ_PROBES = {
    "venue": {
        "api": "venues.name",
        "platform": "venues.name",
        "worker": "identity_sync_jobs.state",
        "identity": "memberships.user_sub",
    },
    "item": {"api": "items.title", "internal": "items.state", "worker": "work_jobs.state"},
    "matching": {"api": "catalog.title", "internal": "matches.id", "worker": "work_jobs.state"},
    "claim": {"api": "claims.state", "worker": "claim_operations.state"},
    "notification": {
        "api": "notifications.state",
        "admin": "notification_jobs.state",
        "worker": "notification_jobs.state",
    },
}


def check(config, services, environment):
    inputs = prepared(config, services, environment)
    for name, (url, passwords) in inputs.items():
        with connect(url) as connection:
            connection.read_only = True
            check_schema(connection, name)
            for mode, role in config.services[name].roles.items():
                check_role(connection, role)
                check_permissions(connection, name, role)
                table, column = READ_PROBES[name][mode].split(".")
                require(
                    connection.execute(
                        "SELECT has_column_privilege(%s,%s,%s,'SELECT')",
                        (role.name, f"{name}.{table}", column),
                    ).fetchone()[0],
                    f"runtime_read_privilege_missing:{name}:{mode}",
                )
                authenticate(url, role, passwords[mode])
            for schema, mode, table, column, privilege in PRIVATE_COLUMNS:
                if schema == name:
                    allowed = connection.execute(
                        "SELECT has_column_privilege(%s,%s,%s,%s)",
                        (
                            config.services[name].roles[mode].name,
                            f"{name}.{table}",
                            column,
                            privilege,
                        ),
                    ).fetchone()[0]
                    require(not allowed, f"private_column_access:{name}:{mode}")
            if name == "matching":
                require(
                    connection.execute(
                        "SELECT 1 FROM pg_extension WHERE extname='vector'",
                    ).fetchone(),
                    "pgvector_missing",
                )
    return {"checked": list(services), "keys": "not_checked_use_keys_command"}


def check_keys():
    raw = secret("FIELD_ENCRYPTION_KEYS")
    active = secret("FIELD_ENCRYPTION_ACTIVE_KEY_ID")
    cipher = FieldCipher.from_secret(raw, active)
    pairs = json.loads(raw, object_pairs_hook=list)
    require(len({name for name, _ in pairs}) == len(pairs), "duplicate_key_ids")
    keys = {name: base64.b64decode(value, validate=True) for name, value in pairs}
    handover = base64.b64decode(secret("HANDOVER_TOKEN_KEY"), validate=True)
    require(len(handover) >= 32, "handover_key_too_short")
    require(
        len(set(keys.values())) == len(keys) and handover not in keys.values(),
        "key_material_reused",
    )
    context = EncryptionContext("claim-service", "claim_contacts", "encrypted_contact", uuid4())
    for key_id in keys:
        previous = FieldCipher(keys, key_id).encrypt(b"deployment-key-self-check", context)
        require(
            cipher.decrypt(previous, context) == b"deployment-key-self-check",
            "key_self_check_failed",
        )
    return {"field_keys": len(keys), "active_key_present": True, "handover_key_valid": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "bootstrap", "migrate", "check", "keys"])
    parser.add_argument("--config", type=Path, default=ROOT / "infra/database/backend.example.json")
    parser.add_argument("--environment", choices=["local", "dev", "prod"], default="dev")
    parser.add_argument("--service", choices=list(MODES), action="append")
    args = parser.parse_args(argv)
    try:
        if args.action == "keys":
            result = check_keys()
        else:
            config = load(args.config)
            selected = set(args.service or MODES)
            services = [name for name in MODES if name in selected]
            if args.action == "plan":
                result = {
                    "database": config.database,
                    "services": {name: config.services[name].model_dump() for name in services},
                    "bootstrap_url_env": config.bootstrap_url_env,
                }
            else:
                result = globals()[args.action](config, services, args.environment)
        print(json.dumps({"status": "ok", "action": args.action, **result}))
        return 0
    except Exception as exc:
        # Pydantic, psycopg and Alembic errors can contain input values or connection details.
        code = str(exc) if isinstance(exc, ToolError) else type(exc).__name__
        print(json.dumps({"status": "failed", "action": args.action, "code": code}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
