"""Operator migration recovery before retrying a schema-blocked CI deployment."""

import argparse
import copy
import json
import os
import re
import sys
from contextlib import closing

import boto3
from botocore.config import Config
from cd_deploy import STATE, migration_hashes, resolve_images
from configure import ROOT, Cluster, outputs, run


def migrate(args, cluster, data, session):
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        raise ValueError("Require the full published source SHA")
    if run(["git", "rev-parse", "HEAD"]).strip() != args.sha:
        raise RuntimeError("Checkout must match the migration source SHA")
    cluster.validate(session, data)
    before = cluster.get("configmap", STATE)
    state = json.loads(before["data"]["state.json"])
    proposed = migration_hashes(args.sha)
    changed = sorted(k for k in proposed if proposed[k] != state["migration_hashes"].get(k))
    if not changed:
        raise RuntimeError("Migration baseline already matches; retry the failed CI job")
    account = data["application"]["value"]["kms_key_arn"].split(":")[4]
    config = Config(connect_timeout=5, read_timeout=30, retries={"mode": "standard"})
    with closing(session.client("ecr", config=config)) as ecr:
        resolve_images(ecr, account, args.region, args.sha)
    print("Running operator migrations; changed schemas: " + ", ".join(changed), flush=True)
    # The established bootstrap verifies role isolation and revokes temporary CREATE
    # grants/deletes its credential namespace in a finalizer, including on failure.
    command = [
        sys.executable,
        str(ROOT / "infra/terraform/scripts/bootstrap-app.py"),
        "--profile",
        args.profile,
        "--region",
        args.region,
        "--kubectl",
        args.kubectl,
        "--kubeconfig",
        args.kubeconfig,
        "--image-sha",
        args.sha,
    ]
    print(run(command), end="", flush=True)
    current = cluster.get("configmap", STATE)
    if current["metadata"]["resourceVersion"] != before["metadata"]["resourceVersion"]:
        raise RuntimeError("CD state changed during migration; operator review required")
    approved = copy.deepcopy(state)
    approved["migration_hashes"] = proposed
    approved["migration_source_commit"] = args.sha
    # Compare-and-swap prevents approving over a concurrently completed release.
    patch = [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": before["metadata"]["resourceVersion"],
        },
        {"op": "replace", "path": "/data/state.json", "value": json.dumps(approved)},
    ]
    run(
        [
            *cluster.command,
            "-n",
            "lostlink",
            "patch",
            "configmap",
            STATE,
            "--type=json",
            "-p",
            json.dumps(patch),
        ]
    )
    print("Migrations verified and baseline approved; deployed source/images unchanged.")
    print("Retry only the failed CI deployment job for " + args.sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--profile", default="lostlink-dev")
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument(
        "--kubectl", default=str(ROOT / "infra/terraform/.terraform/tools/kubectl.exe")
    )
    parser.add_argument(
        "--kubeconfig", default=str(ROOT / "infra/terraform/.terraform/validation/kubeconfig")
    )
    args = parser.parse_args()
    os.environ.update(
        AWS_PROFILE=args.profile, AWS_REGION=args.region, AWS_DEFAULT_REGION=args.region
    )
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    migrate(args, Cluster(args), outputs(), session)


if __name__ == "__main__":
    main()
