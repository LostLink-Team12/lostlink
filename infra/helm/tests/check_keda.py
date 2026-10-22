"""Check that the rendered upstream KEDA operator mounts the PostgreSQL trust root."""

import sys

import yaml


def check(path):
    with open(path, encoding="utf-8") as source:
        docs = [doc for doc in yaml.safe_load_all(source) if doc]
    operator = next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "keda-operator"
    )
    pod = operator["spec"]["template"]["spec"]
    container = next(c for c in pod["containers"] if c["name"] == "keda-operator")
    env = {e["name"]: e.get("value") for e in container.get("env", [])}
    assert env["PGSSLROOTCERT"] == "/etc/lostlink/rds-ca/ca-bundle.pem"
    mount = next(m for m in container["volumeMounts"] if m["name"] == "lostlink-rds-ca")
    assert mount["mountPath"] == "/etc/lostlink/rds-ca" and mount["readOnly"] is True
    volume = next(v for v in pod["volumes"] if v["name"] == "lostlink-rds-ca")
    assert volume["configMap"]["name"] == "lostlink-rds-ca"
    assert "SSL_CERT_FILE" not in env, "Keep system trust for AWS endpoints"
    print("OK: KEDA operator has a read-only RDS CA mount and pgx PGSSLROOTCERT")


if __name__ == "__main__":
    check(sys.argv[1])
