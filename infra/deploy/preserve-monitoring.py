"""Temporarily preserve the two baseline nodes while removing demo-only capacity.

Existing instance protection flags are restored after the Terraform scale-in.
This does not alter the node group's long-term scaling configuration.
"""

import argparse
import json
import os
from pathlib import Path

import boto3
from botocore.config import Config
from configure import LOCAL, Cluster, outputs, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["protect", "restore"])
    args = parser.parse_args()
    os.environ.update(AWS_PROFILE="lostlink-dev", AWS_DEFAULT_REGION="ap-southeast-1")
    root = Path(__file__).resolve().parents[2]
    data = outputs()

    class C:
        kubectl = str(root / "infra/terraform/.terraform/tools/kubectl.exe")
        kubeconfig = str(root / "infra/terraform/.terraform/validation/kubeconfig")

    cluster = Cluster(C())
    session = boto3.Session(profile_name="lostlink-dev", region_name="ap-southeast-1")
    cluster.validate(session, data)
    config = Config(
        connect_timeout=5, read_timeout=20, retries={"mode": "standard", "total_max_attempts": 3}
    )
    eks, scaling = (
        session.client("eks", config=config),
        session.client("autoscaling", config=config),
    )
    try:
        group = eks.describe_nodegroup(
            clusterName=data["cluster_name"]["value"], nodegroupName="general"
        )["nodegroup"]
        name = group["resources"]["autoScalingGroups"][0]["name"]
        instances = scaling.describe_auto_scaling_groups(AutoScalingGroupNames=[name])[
            "AutoScalingGroups"
        ][0]["Instances"]
        existing = {i["InstanceId"]: i["ProtectedFromScaleIn"] for i in instances}
        path = LOCAL / "baseline-instance-protection.json"
        if args.action == "protect":
            if path.exists() and not json.loads(path.read_text()).get("restored"):
                raise RuntimeError(
                    "Baseline protection already recorded; restore it before replacing the record"
                )
            nodes = json.loads(run([*cluster.command, "get", "nodes", "-o", "json"]))["items"]
            oldest = sorted(nodes, key=lambda n: n["metadata"]["creationTimestamp"])[:2]
            ids = [n["spec"]["providerID"].rsplit("/", 1)[1] for n in oldest]
            if len(ids) != 2 or any(i not in existing for i in ids):
                raise RuntimeError("Expected two baseline instances in the Terraform node group")
            record = {"group": name, "previous": {i: existing[i] for i in ids}, "restored": False}
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            scaling.set_instance_protection(
                InstanceIds=ids, AutoScalingGroupName=name, ProtectedFromScaleIn=True
            )
            print("Protected the two baseline nodes for demo capacity reduction")
        else:
            record = json.loads(path.read_text())
            if record["group"] != name:
                raise RuntimeError("Protection record belongs to a different node group")
            for flag in [False, True]:
                ids = [
                    i
                    for i, previous in record["previous"].items()
                    if previous == flag and i in existing
                ]
                if ids:
                    scaling.set_instance_protection(
                        InstanceIds=ids, AutoScalingGroupName=name, ProtectedFromScaleIn=flag
                    )
            record["restored"] = True
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            print("Restored the baseline instances' original scale-in protection flags")
    finally:
        eks.close()
        scaling.close()


if __name__ == "__main__":
    main()
