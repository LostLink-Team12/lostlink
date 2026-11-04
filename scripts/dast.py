"""Build this checkout and scan a disposable, private Docker candidate stack."""

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from jwt.algorithms import RSAAlgorithm

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/security/dast"
SERVICES = ("venue", "item", "matching", "claim", "notification")
ZAP_IMAGE = (
    "ghcr.io/zaproxy/zaproxy:2.17.0@sha256:"
    "9624669db72f54d86c4b46a7064518bd9bf79d8c61a4c31befa9923489f328af"
)


def command(args, *, check=True, timeout=1800):
    result = subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed ({result.returncode})")
    return result


def prepare(private):
    private.mkdir(parents=True, exist_ok=True)
    authority = private / "authority"
    authority.mkdir(exist_ok=True)
    private = private / "transport"
    private.mkdir(exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Ephemeral DAST CA")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(n)
                    for n in [
                        "frontend",
                        "identity",
                        "localhost",
                        *[f"{s}-api.lostlink.svc.cluster.local" for s in SERVICES],
                    ]
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM)
    (private / "ca.pem").write_bytes(pem)
    (private / "server.pem").write_bytes(pem)
    (private / "server.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (authority / "jwk.json").write_text(RSAAlgorithm.to_jwk(signing_key), encoding="utf-8")
    gateway = (ROOT / "infra/deploy/gateway.conf").read_text(encoding="utf-8")
    gateway = gateway.replace(
        "listen 8080;",
        "listen 8443 ssl;\n    ssl_certificate /private/server.pem;\n"
        "    ssl_certificate_key /private/server.key;",
    )
    gateway = gateway.replace("kube-dns.kube-system.svc.cluster.local", "127.0.0.11")
    gateway = gateway.replace("proxy_pass https://$backend;", "proxy_pass https://$backend:8443;")
    (private / "gateway.conf").write_text(gateway, encoding="utf-8")
    (private / "combined.pem").write_bytes(pem)
    (private / "config.json").write_text(
        json.dumps(
            {
                "api": {s: f"/api/{s}" for s in SERVICES},
                "auth": {
                    "domain": "https://identity:8443",
                    "ownerClientId": "dast-owner",
                    "venueClientId": "dast-staff",
                },
            }
        ),
        encoding="utf-8",
    )


def configuration(private, public, tag):
    def mount(src, dest, readonly=True):
        return {"type": "bind", "source": str(src), "target": dest, "read_only": readonly}

    images = {s: f"lostlink/{s}-service:{tag}" for s in SERVICES}
    runner_uid = getattr(os, "getuid", lambda: 1000)()
    runner_gid = getattr(os, "getgid", lambda: 1000)()
    authority = private / "authority"
    private = private / "transport"
    common = {
        "ENVIRONMENT": "test",
        "COGNITO_ISSUER": "https://identity:8443",
        "COGNITO_AUDIENCE": "dast-api",
        "COGNITO_CLIENT_IDS": json.dumps(
            ["dast-owner", "dast-staff", "dast-platform", "dast-machine"]
        ),
        "USER_CLIENT_IDS": '["dast-owner","dast-staff"]',
        "SSL_CERT_FILE": "/private/ca.pem",
        "DB_POOL_SIZE": "1",
        "DB_MAX_OVERFLOW": "0",
        "AWS_EC2_METADATA_DISABLED": "true",
    }

    def database(name):
        return f"postgresql+psycopg://lostlink_{name}:local-development-only@postgres:5432/lostlink"

    services = {
        "postgres": {
            "image": "pgvector/pgvector:pg16",
            "environment": {
                "POSTGRES_USER": "lostlink",
                "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
                "POSTGRES_DB": "lostlink",
            },
            "volumes": [
                mount(ROOT / "scripts/init-local.sql", "/docker-entrypoint-initdb.d/roles.sql")
            ],
            "tmpfs": ["/var/lib/postgresql/data"],
            "healthcheck": {
                "test": ["CMD", "pg_isready", "-U", "lostlink"],
                "interval": "2s",
                "retries": 60,
            },
        },
        "localstack": {
            "image": "localstack/localstack:3",
            "environment": {"SERVICES": "s3"},
            "healthcheck": {
                "test": ["CMD", "curl", "-f", "http://localhost:4566/_localstack/health"],
                "interval": "3s",
                "retries": 60,
            },
        },
        "identity": {
            "image": images["venue"],
            "command": ["python", "/dast/identity.py"],
            "healthcheck": {"disable": True},
            "volumes": [
                mount(private, "/private"),
                mount(authority, "/authority"),
                mount(FIXTURES, "/dast"),
            ],
        },
        "probe": {
            "image": images["venue"],
            # The Linux runner owns public/. Match it instead of relying on
            # Windows bind mounts' permissive ownership semantics.
            "user": f"{runner_uid}:{runner_gid}",
            "healthcheck": {"disable": True},
            "environment": {"SSL_CERT_FILE": "/private/ca.pem"},
            "volumes": [
                mount(private, "/private"),
                mount(authority, "/authority"),
                mount(FIXTURES, "/dast"),
                mount(public, "/evidence", False),
                mount(ROOT / "docs/api", "/contracts"),
            ],
        },
        "zap": {
            "image": ZAP_IMAGE,
            "environment": {"JAVA_OPTS": "-Xmx1024m"},
            "command": [
                "zap.sh",
                "-daemon",
                "-host",
                "0.0.0.0",
                "-port",
                "8080",
                "-config",
                "api.disablekey=true",
                "-config",
                "api.addrs.addr.name=.*",
                "-config",
                "api.addrs.addr.regex=true",
            ],
        },
        "frontend": {
            "image": f"lostlink/frontend:{tag}",
            "read_only": True,
            "tmpfs": ["/tmp"],
            "volumes": [
                mount(private, "/private"),
                mount(private / "gateway.conf", "/etc/nginx/conf.d/default.conf"),
                mount(private, "/trust"),
                mount(private, "/public-config"),
            ],
        },
    }
    for service in SERVICES:
        env = {**common, "DATABASE_URL": database(service + "_api")}
        if service == "venue":
            env.update(
                PLATFORM_DATABASE_URL=database("venue_platform"),
                PLATFORM_CLIENT_IDS='["dast-platform"]',
                MEMBER_CHECK_CLIENT_IDS='["dast-machine"]',
            )
        if service in {"item", "matching", "claim"}:
            env.update(
                MACHINE_TOKEN_URL="https://identity:8443/oauth2/token",
                MACHINE_CLIENT_ID="dast-machine",
                MACHINE_CLIENT_SECRET="ephemeral-test-only",
                VENUE_BASE_URL="https://venue-api.lostlink.svc.cluster.local:8443",
                ITEM_BASE_URL="https://item-api.lostlink.svc.cluster.local:8443",
            )
        if service in {"item", "matching"}:
            env["INTERNAL_DATABASE_URL"] = database(service + "_internal")
        if service in {"matching", "notification"}:
            env["USER_CLIENT_IDS"] = '["dast-owner"]'
        if service == "item":
            env.update(PHOTO_BUCKET="dast-media", AWS_ENDPOINT_URL="http://localstack:4566")
        if service == "claim":
            env.update(
                FIELD_ENCRYPTION_KEYS=json.dumps(
                    {"dast": base64.b64encode(secrets.token_bytes(32)).decode()}
                ),
                FIELD_ENCRYPTION_ACTIVE_KEY_ID="dast",
                HANDOVER_TOKEN_KEY=base64.b64encode(secrets.token_bytes(32)).decode(),
            )
        services[service] = {
            "image": images[service],
            "environment": env,
            "healthcheck": {"disable": True},
            "command": [
                "uvicorn",
                f"{service}_app.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8443",
                "--ssl-certfile",
                "/private/server.pem",
                "--ssl-keyfile",
                "/private/server.key",
                "--no-access-log",
            ],
            "networks": {"default": {"aliases": [f"{service}-api.lostlink.svc.cluster.local"]}},
            "volumes": [
                mount(private, "/private"),
            ],
        }
    return {"services": services, "networks": {"default": {"internal": True}}}


def cleanup(base):
    private = base / "private"
    state = private / "state.json"
    if not state.exists():
        return not (private / "compose.json").exists()
    project = json.loads(state.read_text(encoding="utf-8"))["project"]
    if not re.fullmatch(r"ll-dast-[a-f0-9]{12}", project):
        raise ValueError("Refuse to clean an unrelated Docker project")
    result = command(
        [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(private / "compose.json"),
            "down",
            "--volumes",
            "--remove-orphans",
        ],
        check=False,
        timeout=120,
    )
    if result.returncode:
        return False
    for name in ("authority/jwk.json", "transport/server.key", "compose.json", "state.json"):
        (private / name).unlink(missing_ok=True)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha")
    parser.add_argument("--output", type=Path, default=ROOT / "build/dast")
    parser.add_argument(
        "--skip-build", action="store_true", help="Local retry with already-built candidate images"
    )
    parser.add_argument(
        "--cleanup", action="store_true", help="Retry cleanup of only this owned stack"
    )
    args = parser.parse_args(argv)
    if args.cleanup:
        return int(not cleanup(args.output.resolve()))
    if not args.sha or not re.fullmatch(r"[a-f0-9]{40}", args.sha):
        raise ValueError("Require a full candidate source SHA")
    tag = "dast-" + args.sha
    base = args.output.resolve()
    private, public = base / "private", base / "public"
    public.mkdir(parents=True, exist_ok=True)
    project = "ll-dast-" + secrets.token_hex(6)
    compose = ["docker", "compose", "-p", project, "-f", str(private / "compose.json")]
    output = {"source_sha": args.sha, "gate": "error"}
    cleanup_ok = True
    try:
        if command(["git", "rev-parse", "HEAD"]).stdout.strip() != args.sha:
            raise ValueError("Candidate SHA does not match checkout")
        if not args.skip_build:
            for service in (*SERVICES, "frontend"):
                print(f"Building DAST candidate: {service}", flush=True)
                dockerfile = (
                    "infra/deploy/frontend.Dockerfile"
                    if service == "frontend"
                    else f"services/{service}-service/Dockerfile"
                )
                image_name = service if service == "frontend" else service + "-service"
                command(
                    [
                        "docker",
                        "build",
                        "--label",
                        f"org.opencontainers.image.revision={args.sha}",
                        "-f",
                        dockerfile,
                        "-t",
                        f"lostlink/{image_name}:{tag}",
                        ".",
                    ]
                )
        (public / "result.json").unlink(missing_ok=True)
        prepare(private)
        config = configuration(private, public, tag)
        (private / "compose.json").write_text(json.dumps(config), encoding="utf-8")
        (private / "state.json").write_text(json.dumps({"project": project}), encoding="utf-8")
        admin = config["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"]
        command([*compose, "up", "-d", "--wait", "postgres", "localstack"])
        for service in SERVICES:
            print(f"Migrating isolated candidate: {service}", flush=True)
            command(
                [
                    *compose,
                    "run",
                    "--rm",
                    "--no-deps",
                    "-e",
                    f"MIGRATION_DATABASE_URL=postgresql+psycopg://lostlink:{admin}@postgres:5432/lostlink",
                    service,
                    "alembic",
                    "upgrade",
                    "head",
                ]
            )
        command(
            [*compose, "run", "--rm", "--no-deps", "probe", "python", "/dast/probe.py", "bootstrap"]
        )
        command(
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "-e",
                f"DAST_ADMIN_DSN=postgresql://lostlink:{admin}@postgres:5432/lostlink",
                "probe",
                "python",
                "/dast/seed.py",
            ]
        )
        command([*compose, "up", "-d", "identity", "frontend", "zap", *SERVICES])
        print("Scanning isolated candidate through ZAP", flush=True)
        result = command(
            [*compose, "run", "--rm", "--no-deps", "probe", "python", "/dast/probe.py"],
            check=False,
            timeout=900,
        )
        output.update(json.loads((public / "result.json").read_text(encoding="utf-8")))
        if result.returncode and output["gate"] == "passed":
            output["gate"] = "error"
        output["images"] = {
            s: command(
                [
                    "docker",
                    "image",
                    "inspect",
                    config["services"][s]["image"],
                    "--format",
                    "{{.Id}}",
                ]
            ).stdout.strip()
            for s in (*SERVICES, "frontend")
        }
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
        output["error"] = type(exc).__name__
    finally:
        try:
            cleanup_ok = cleanup(base)
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            cleanup_ok = False
        output["cleanup"] = cleanup_ok
        if not cleanup_ok:
            output["gate"] = "error"
        (public / "result.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"DAST: {output['gate']}; cleanup: {cleanup_ok}", flush=True)
    return int(output["gate"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
