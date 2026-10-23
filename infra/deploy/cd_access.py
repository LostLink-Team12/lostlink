"""Lease only this runner's /32 on the existing dev EKS public endpoint."""

import argparse
import ipaddress
import json
import time
from contextlib import closing
from pathlib import Path
from urllib.request import urlopen

import boto3
from botocore.config import Config


def wait_active(eks, cluster):
    for _ in range(180):
        value = eks.describe_cluster(name=cluster)["cluster"]
        if value["status"] == "ACTIVE":
            return value
        time.sleep(5)
    raise RuntimeError("EKS did not become ACTIVE")


def change(eks, cluster, cidrs):
    result = eks.update_cluster_config(
        name=cluster, resourcesVpcConfig={"publicAccessCidrs": cidrs}
    )
    for _ in range(180):
        status = eks.describe_update(name=cluster, updateId=result["update"]["id"])["update"]
        if status["status"] == "Successful":
            return
        if status["status"] in {"Failed", "Cancelled"}:
            raise RuntimeError("EKS endpoint access update failed")
        time.sleep(5)
    raise RuntimeError("EKS endpoint access update timed out; run lease cleanup")


def open_access(eks, cluster, address, path):
    ip = ipaddress.ip_address(address.strip())
    if ip.version != 4 or not ip.is_global:
        raise ValueError("Runner must have a global IPv4 address")
    config = wait_active(eks, cluster)["resourcesVpcConfig"]
    if not config["endpointPublicAccess"] or not config["endpointPrivateAccess"]:
        raise RuntimeError("Keep both the existing public and private endpoints enabled")
    current = config["publicAccessCidrs"]
    if any(ipaddress.ip_network(c).prefixlen < 24 for c in current):
        raise RuntimeError("Refuse a broadly exposed EKS endpoint")
    cidr = str(ip) + "/32"
    added = not any(ip in ipaddress.ip_network(c) for c in current)
    # Persist before the API call, so an interrupted update can still be cleaned up.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cluster": cluster, "cidr": cidr, "added": added}))
    if added:
        change(eks, cluster, [*current, cidr])


def close_access(eks, path):
    if not path.exists():
        return
    lease = json.loads(path.read_text())
    if lease["added"]:
        current = wait_active(eks, lease["cluster"])["resourcesVpcConfig"]["publicAccessCidrs"]
        remaining = [c for c in current if c != lease["cidr"]]
        if not remaining:
            raise RuntimeError("Refuse to remove the last EKS allowlist entry")
        if remaining != current:
            change(eks, lease["cluster"], remaining)
    path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["open", "close"])
    parser.add_argument("--cluster", default="lostlink-dev", choices=["lostlink-dev"])
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--lease", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "close" and not args.lease.exists():
        print("No runner access lease; cleanup skipped")
        return
    with closing(
        boto3.client(
            "eks",
            region_name=args.region,
            config=Config(connect_timeout=5, read_timeout=30, retries={"mode": "standard"}),
        )
    ) as eks:
        if args.action == "open":
            with urlopen("https://checkip.amazonaws.com", timeout=15) as response:
                address = response.read(64).decode()
            print("Runner public IPv4 /32: " + address.strip() + "/32", flush=True)
            open_access(eks, args.cluster, address, args.lease)
        else:
            close_access(eks, args.lease)
    print("Dev EKS runner access " + args.action + " completed")


if __name__ == "__main__":
    main()
