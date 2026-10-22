"""Prepare process-isolated dev configuration without persisting credentials locally.

Run with the locked repository dependencies plus PyYAML. Uses the existing
lostlink-dev AWS profile and an explicit, repository-local kubeconfig.
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

import boto3
import yaml
from botocore.config import Config
from botocore.httpsession import get_cert_path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from values import SERVICES, helm_values

ROOT = Path(__file__).resolve().parents[2]
LOCAL = Path(__file__).resolve().parent / ".local"


def run(args, data=None):
    result = subprocess.run(
        [str(a) for a in args], input=data, text=True, capture_output=True, check=False
    )
    if result.returncode:
        # kubectl can repeat submitted Secret values in errors. Never echo them.
        raise RuntimeError(f"Command failed: {args[0]} {args[1]}")
    return result.stdout


def outputs():
    return json.loads(
        run(["terraform", f"-chdir={ROOT / 'infra/terraform/environments/dev'}", "output", "-json"])
    )


class Cluster:
    def __init__(self, args):
        self.command = [args.kubectl, "--kubeconfig", args.kubeconfig, "--context", "lostlink-dev"]

    def apply(self, obj):
        run(
            [*self.command, "apply", "--server-side", "--field-manager=lostlink-c3", "-f", "-"],
            json.dumps(obj),
        )

    def get(self, kind, name, namespace="lostlink"):
        result = run(
            [*self.command, "get", kind, name, "-n", namespace, "--ignore-not-found", "-o", "json"]
        )
        return json.loads(result) if result.strip() else None

    def configmap(self, name, data, namespace="lostlink"):
        self.apply(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": namespace},
                "data": data,
            }
        )

    def secret(self, name, data, namespace="lostlink", secret_type="Opaque"):
        # Avoid apply's last-applied annotation containing a second credential copy.
        obj = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": secret_type,
            "metadata": {"name": name, "namespace": namespace},
            "stringData": data,
        }
        run(
            [*self.command, "apply", "--server-side", "--field-manager=lostlink-c3", "-f", "-"],
            json.dumps(obj),
        )

    def validate(self, session, data):
        config = Config(
            connect_timeout=5,
            read_timeout=15,
            retries={"mode": "standard", "total_max_attempts": 3},
        )
        sts, eks = session.client("sts", config=config), session.client("eks", config=config)
        try:
            account = data["application"]["value"]["kms_key_arn"].split(":")[4]
            identity = sts.get_caller_identity()
            if identity["Account"] != account or identity["Arn"].endswith(":root"):
                raise RuntimeError("Use the dev account's non-root deployment identity")
            endpoint = eks.describe_cluster(name=data["cluster_name"]["value"])["cluster"][
                "endpoint"
            ]
            context = json.loads(run([*self.command, "config", "view", "--minify", "-o", "json"]))
            if context["clusters"][0]["cluster"]["server"] != endpoint:
                raise RuntimeError("Kubeconfig does not point to the Terraform EKS cluster")
        finally:
            sts.close()
            eks.close()


def certificates():
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "LostLink dev internal CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = [f"{service}-api.lostlink.svc.cluster.local" for service in SERVICES]
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=60))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    return {
        "tls.crt": cert.public_bytes(serialization.Encoding.PEM).decode(),
        "tls.key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "ca.crt": ca.public_bytes(serialization.Encoding.PEM).decode(),
    }


def prepare(cluster, data):
    for namespace in ["lostlink", "keda"]:
        cluster.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}})
    existing = cluster.get("secret", "lostlink-internal-tls")
    if existing:
        tls = {k: base64.b64decode(v).decode() for k, v in existing["data"].items()}
        cert = x509.load_pem_x509_certificate(tls["tls.crt"].encode())
        if cert.not_valid_after_utc < datetime.now(UTC) + timedelta(days=7):
            raise RuntimeError(
                "Internal certificate expires soon; rotate it and restart all TLS users"
            )
    else:
        tls = certificates()
        cluster.secret("lostlink-internal-tls", tls, secret_type="kubernetes.io/tls")
    with urlopen(
        "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem", timeout=30
    ) as r:
        rds = r.read().decode()
    if "BEGIN CERTIFICATE" not in rds:
        raise RuntimeError("Invalid RDS CA bundle")
    trust = {"rds.pem": rds, "combined.pem": Path(get_cert_path(True)).read_text() + tls["ca.crt"]}
    cluster.configmap("lostlink-trust", trust)
    cluster.configmap("lostlink-trust", {"rds.pem": rds}, namespace="keda")
    files = {
        name: (ROOT / "infra/deploy" / name).read_text(encoding="utf-8")
        for name in ["runtime.py", "worker-probe.py", "backlog.py"]
    }
    files["prepare_clip_model.py"] = (ROOT / "scripts/prepare_clip_model.py").read_text(
        encoding="utf-8"
    )
    cluster.configmap("lostlink-runtime", files)
    cluster.configmap(
        "lostlink-api-tls",
        {"default.conf": (ROOT / "infra/deploy/api-tls.conf").read_text(encoding="utf-8")},
    )
    app = data["application"]["value"]
    public = {
        "api": {s: f"/api/{s}" for s in ["venue", "item", "matching", "claim"]},
        "auth": {
            "domain": data["cognito"]["value"]["login_domain"],
            "ownerClientId": app["web_client_id"],
            "venueClientId": app["operator_client_id"],
        },
    }
    cluster.configmap("lostlink-public-config", {"config.json": json.dumps(public)})
    cluster.configmap(
        "lostlink-dashboards",
        {"scaling.json": (ROOT / "infra/deploy/dashboard.json").read_text(encoding="utf-8")},
    )
    if not cluster.get("secret", "lostlink-grafana"):
        cluster.secret("lostlink-grafana", {"password": secrets.token_urlsafe(36)})
    print("Prepared runtime, TLS trust, public frontend configuration and private dashboards")


def monitor_role(cluster, data, session, image):
    existing = cluster.get("secret", "lostlink-backlog")
    password = (
        base64.b64decode(existing["data"]["password"]).decode()
        if existing
        else secrets.token_urlsafe(36)
    )
    db = data["database"]["value"]
    sm = session.client(
        "secretsmanager",
        config=Config(
            connect_timeout=5,
            read_timeout=15,
            retries={"mode": "standard", "total_max_attempts": 3},
        ),
    )
    try:
        admin = json.loads(sm.get_secret_value(SecretId=db["master_secret_arn"])["SecretString"])
    finally:
        sm.close()
    namespace = "lostlink-monitor-bootstrap-" + secrets.token_hex(4)
    cluster.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}})
    try:
        cluster.secret(
            "bootstrap",
            {
                "ADMIN_JSON": json.dumps(
                    {**admin, "host": db["host"], "port": db["port"], "dbname": db["name"]}
                ),
                "MONITOR_PASSWORD": password,
            },
            namespace,
        )
        trust = cluster.get("configmap", "lostlink-trust")["data"]
        cluster.configmap("trust", {"rds.pem": trust["rds.pem"]}, namespace)
        script = """
import json, os
import psycopg
from psycopg import sql
d=json.loads(os.environ['ADMIN_JSON'])
with psycopg.connect(user=d['username'],password=d['password'],host=d['host'],
                    port=d['port'],dbname=d['dbname'],sslmode='verify-full',
                    sslrootcert='/trust/rds.pem',connect_timeout=10) as c:
    role = 'lostlink_matching_monitor'
    if not c.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():
        c.execute('CREATE ROLE lostlink_matching_monitor LOGIN '
                  'NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS')
    c.execute(sql.SQL('ALTER ROLE lostlink_matching_monitor PASSWORD {}').format(
        sql.Literal(os.environ['MONITOR_PASSWORD'])))
    c.execute('ALTER ROLE lostlink_matching_monitor SET default_transaction_read_only=on')
    c.execute('ALTER ROLE lostlink_matching_monitor SET statement_timeout=5000')
    c.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO lostlink_matching_monitor').format(
        sql.Identifier(d['dbname'])))
    c.execute('SET LOCAL ROLE lostlink_matching_migration')
    c.execute('GRANT USAGE ON SCHEMA matching TO lostlink_matching_monitor')
    c.execute('GRANT SELECT(state,kind,available_at) '
              'ON matching.work_jobs TO lostlink_matching_monitor')
    c.execute('DROP POLICY IF EXISTS work_jobs_monitor ON matching.work_jobs')
    c.execute('CREATE POLICY work_jobs_monitor ON matching.work_jobs FOR SELECT '
              'TO lostlink_matching_monitor USING (true)')
print('Read-only embedding monitor role configured')
"""
        cluster.apply(
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "bootstrap", "namespace": namespace},
                "spec": {
                    "backoffLimit": 0,
                    "activeDeadlineSeconds": 180,
                    "template": {
                        "spec": {
                            "restartPolicy": "Never",
                            "automountServiceAccountToken": False,
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 1000,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "containers": [
                                {
                                    "name": "bootstrap",
                                    "image": image,
                                    "command": ["python", "-c", script],
                                    "envFrom": [{"secretRef": {"name": "bootstrap"}}],
                                    "securityContext": {
                                        "readOnlyRootFilesystem": True,
                                        "allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                    "resources": {
                                        "requests": {"cpu": "25m", "memory": "64Mi"},
                                        "limits": {"cpu": "250m", "memory": "128Mi"},
                                    },
                                    "volumeMounts": [
                                        {"name": "trust", "mountPath": "/trust", "readOnly": True}
                                    ],
                                }
                            ],
                            "volumes": [{"name": "trust", "configMap": {"name": "trust"}}],
                        }
                    },
                },
            }
        )
        run(
            [
                *cluster.command,
                "wait",
                "--for=condition=complete",
                "job/bootstrap",
                "-n",
                namespace,
                "--timeout=180s",
            ]
        )
        connection = (
            f"postgresql://lostlink_matching_monitor:{quote(password, safe='')}@"
            f"{db['host']}:{db['port']}/{db['name']}?sslmode=verify-full&sslrootcert=/trust/rds.pem"
        )
        cluster.secret("lostlink-backlog", {"connection": connection, "password": password})
        print("Installed restricted backlog credentials; no application or migration login reused")
    finally:
        run([*cluster.command, "delete", "namespace", namespace, "--wait=false"])


def write_values(data, images):
    infra = json.loads((LOCAL / "infrastructure.tfvars.json").read_text())
    origin = (data.get("c3", {}).get("value") or {}).get("frontend_origin")
    revision = hashlib.sha256(
        b"".join(
            (ROOT / "infra/deploy" / f).read_bytes()
            for f in ["runtime.py", "worker-probe.py", "backlog.py"]
        )
    ).hexdigest()
    for service, values in helm_values(
        data, images, origin=origin, revision=revision, proxy_image=images["proxy"]
    ).items():
        (LOCAL / f"{service}.yaml").write_text(
            yaml.safe_dump(values, sort_keys=False), encoding="utf-8"
        )
    c3 = data["c3"]["value"]
    platform = {
        "frontendImage": images["frontend"],
        "metricsImage": images["venue"],
        "albSecurityGroup": c3["alb_security_group_id"],
        "subnets": data["private_subnet_ids"]["value"],
        "originToken": infra["c3_origin_token"],
    }
    (LOCAL / "platform.yaml").write_text(yaml.safe_dump(platform), encoding="utf-8")
    print("Wrote public Helm values and local-only ingress token")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["infrastructure", "prepare", "monitor-role", "values"])
    parser.add_argument("--profile", default="lostlink-dev")
    parser.add_argument(
        "--kubectl", default=str(ROOT / "infra/terraform/.terraform/tools/kubectl.exe")
    )
    parser.add_argument(
        "--kubeconfig", default=str(ROOT / "infra/terraform/.terraform/validation/kubeconfig")
    )
    args = parser.parse_args()
    os.environ.update(AWS_PROFILE=args.profile, AWS_DEFAULT_REGION="ap-southeast-1")
    LOCAL.mkdir(exist_ok=True)
    if args.action == "infrastructure":
        path = LOCAL / "infrastructure.tfvars.json"
        if not path.exists():
            path.write_text(
                json.dumps(
                    {
                        "c3_enabled": True,
                        "node_desired_size": 2,
                        "node_max_size": 5,
                        "c3_origin_token": secrets.token_urlsafe(48),
                    }
                ),
                encoding="utf-8",
            )
        print("Prepared ignored C3 Terraform input; two nodes bound application replica capacity")
        return
    data = outputs()
    cluster = Cluster(args)
    session = boto3.Session(profile_name=args.profile, region_name="ap-southeast-1")
    if args.action != "values":
        cluster.validate(session, data)
    if args.action == "prepare":
        prepare(cluster, data)
    else:
        images = json.loads((LOCAL / "images.json").read_text())
        if args.action == "values":
            write_values(data, images)
        else:
            monitor_role(cluster, data, session, images["venue"])


if __name__ == "__main__":
    main()
