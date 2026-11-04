"""Record/validate only deployed image digests; never publish Kubernetes state."""

import argparse
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

SERVICES = ("venue", "item", "matching", "claim", "notification", "frontend")


def validate(data, account, region, source=None):
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(
        r"[a-z]{2}-[a-z]+-[0-9]+", region
    ):
        raise ValueError("Invalid registry configuration")
    sha = data["source_commit"]
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or source and sha != source:
        raise ValueError("Deployment source does not match trusted run")
    if set(data["images"]) != set(SERVICES):
        raise ValueError("Require the complete deployed image set")
    matrix = []
    for service in SERVICES:
        name = service if service == "frontend" else service + "-service"
        image = data["images"][service]
        prefix = f"{account}.dkr.ecr.{region}.amazonaws.com/lostlink/{name}@sha256:"
        if not image.startswith(prefix) or not re.fullmatch(r"[a-f0-9]{64}", image[len(prefix) :]):
            raise ValueError("Expected an allowlisted image pinned by digest")
        profile = (
            "frontend-image"
            if service == "frontend"
            else "matching-image"
            if service == "matching"
            else "python-image"
        )
        matrix.append({"name": name, "image": image, "profile": profile})
    return matrix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--kubeconfig")
    parser.add_argument("--matrix-output", action="store_true")
    args = parser.parse_args(argv)
    if args.capture:
        state = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                args.kubeconfig,
                "--context",
                "lostlink-dev",
                "-n",
                "lostlink",
                "get",
                "configmap",
                "lostlink-cd-state",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        deployed = json.loads(json.loads(state.stdout)["data"]["state.json"])
        data = {
            "source_commit": deployed["source_commit"],
            "images": deployed["images"],
            "recorded_at": datetime.now(UTC).isoformat(),
        }
    else:
        data = json.loads(args.manifest.read_text(encoding="utf-8"))
    matrix = validate(data, args.account, args.region, args.source)
    if args.capture:
        args.manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if args.matrix_output:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("images=" + json.dumps(matrix, separators=(",", ":")) + "\n")
    print("Validated six deployment digests from the trusted main run")


if __name__ == "__main__":
    main()
