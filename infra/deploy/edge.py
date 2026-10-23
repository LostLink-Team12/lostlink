"""Resolve the private ALB into the ignored Terraform input for the HTTPS edge."""

import json
import os
from pathlib import Path

import boto3
from botocore.config import Config
from configure import Cluster


def main():
    root = Path(__file__).resolve().parents[2]
    os.environ["AWS_PROFILE"] = "lostlink-dev"

    class Args:
        kubectl = str(root / "infra/terraform/.terraform/tools/kubectl.exe")
        kubeconfig = str(root / "infra/terraform/.terraform/validation/kubeconfig")

    ingress = Cluster(Args()).get("ingress", "lostlink")
    addresses = ingress.get("status", {}).get("loadBalancer", {}).get("ingress", [])
    if not addresses:
        raise RuntimeError("Wait for the private Ingress ALB to become active")
    hostname = addresses[0]["hostname"]
    session = boto3.Session(profile_name="lostlink-dev", region_name="ap-southeast-1")
    client = session.client(
        "elbv2",
        config=Config(
            connect_timeout=5,
            read_timeout=15,
            retries={"mode": "standard", "total_max_attempts": 3},
        ),
    )
    try:
        matches = [
            lb
            for page in client.get_paginator("describe_load_balancers").paginate()
            for lb in page["LoadBalancers"]
            if lb["DNSName"] == hostname
        ]
    finally:
        client.close()
    if len(matches) != 1 or matches[0]["Scheme"] != "internal":
        raise RuntimeError("Expected exactly one private ALB")
    if matches[0]["State"]["Code"] != "active":
        raise RuntimeError("Wait for the private ALB to become active")
    path = Path(__file__).resolve().parent / ".local/infrastructure.tfvars.json"
    values = json.loads(path.read_text())
    values.update(c3_alb_dns_name=hostname, c3_alb_arn=matches[0]["LoadBalancerArn"])
    path.write_text(json.dumps(values, indent=2), encoding="utf-8")
    print("Recorded the active private ALB for CloudFront VPC origin provisioning")


if __name__ == "__main__":
    main()
