"""Operator approves a deployed migration baseline and installs named CD RBAC."""

import argparse
import json
from pathlib import Path

import boto3
from cd_deploy import SERVICES, STATE, migration_hashes
from configure import ROOT, Cluster, outputs, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--profile", default="lostlink-dev")
    parser.add_argument(
        "--kubectl", default=str(ROOT / "infra/terraform/.terraform/tools/kubectl.exe")
    )
    parser.add_argument(
        "--kubeconfig", default=str(ROOT / "infra/terraform/.terraform/validation/kubeconfig")
    )
    args = parser.parse_args()
    data, cluster = outputs(), Cluster(args)
    session = boto3.Session(profile_name=args.profile, region_name="ap-southeast-1")
    cluster.validate(session, data)
    evidence = json.loads(args.manifest.read_text(encoding="utf-8"))
    if evidence["source_commit"] != args.source:
        raise RuntimeError("Manifest must identify the approved deployed source")
    for service in SERVICES:
        value = cluster.get("deployment", service + "-api")
        actual = next(
            c["image"]
            for c in value["spec"]["template"]["spec"]["containers"]
            if c["name"] == "app"
        )
        if actual != evidence["images"][service]:
            raise RuntimeError("Deployed image differs from reviewed manifest: " + service)
    run([*cluster.command, "apply", "-f", str(ROOT / "infra/deploy/cd-rbac.yaml")])
    cluster.configmap(
        STATE,
        {
            "state.json": json.dumps(
                {
                    "source_commit": args.source,
                    "migration_hashes": migration_hashes(args.source),
                    "origin": data["c3"]["value"]["frontend_origin"],
                    "images": evidence["images"],
                }
            )
        },
    )
    print("Approved deployed schema baseline; installed named Deployment/ConfigMap patch access")


if __name__ == "__main__":
    main()
