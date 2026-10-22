"""Build current application code and publish digest-pinned C3 images to ECR."""

import argparse
import base64
import hashlib
import json
import subprocess
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[2]
LOCAL = Path(__file__).resolve().parent / ".local"


def run(args, *, capture=False, data=None):
    return subprocess.run(
        args,
        cwd=ROOT,
        input=data,
        text=True,
        check=True,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="lostlink-dev")
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name="ap-southeast-1")
    config = Config(
        connect_timeout=5, read_timeout=30, retries={"mode": "standard", "total_max_attempts": 3}
    )
    ecr = session.client("ecr", config=config)
    auth = ecr.get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    run(
        ["docker", "login", "--username", user, "--password-stdin", auth["proxyEndpoint"]],
        data=password,
    )
    registry = auth["proxyEndpoint"].removeprefix("https://")
    base = ecr.describe_images(
        repositoryName="lostlink/matching-service",
        imageIds=[{"imageTag": "137cd00e6a540f7f1a46b19c6a44baa2b68343d4"}],
    )
    ml_base = registry + "/lostlink/matching-service@" + base["imageDetails"][0]["imageDigest"]
    images = {}
    for service in ["venue", "item", "claim", "notification", "matching", "frontend"]:
        repository = "lostlink/" + (service if service == "frontend" else service + "-service")
        tag = f"{registry}/{repository}:{args.tag}"
        dockerfile = (
            "infra/deploy/frontend.Dockerfile"
            if service == "frontend"
            else "infra/deploy/matching.Dockerfile"
            if service == "matching"
            else f"services/{service}-service/Dockerfile"
        )
        command = ["docker", "build", "--pull", "-f", dockerfile, "-t", tag]
        if service == "matching":
            command += ["--build-arg", f"BASE_IMAGE={ml_base}"]
        run([*command, "."])
        run(["docker", "push", tag])
        details = ecr.describe_images(repositoryName=repository, imageIds=[{"imageTag": args.tag}])
        images[service] = f"{registry}/{repository}@{details['imageDetails'][0]['imageDigest']}"
        LOCAL.mkdir(exist_ok=True)
        (LOCAL / "images.json").write_text(json.dumps(images, indent=2), encoding="utf-8")
    # Resolve the TLS sidecar to a digest as well; never deploy a floating tag.
    run(["docker", "pull", "nginxinc/nginx-unprivileged:stable-alpine"])
    images["proxy"] = json.loads(
        run(
            [
                "docker",
                "image",
                "inspect",
                "nginxinc/nginx-unprivileged:stable-alpine",
                "--format",
                "{{json .RepoDigests}}",
            ],
            capture=True,
        )
    )[0]
    (LOCAL / "images.json").write_text(json.dumps(images, indent=2), encoding="utf-8")
    source = run(["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"], capture=True).strip()
    diff = run(["git", "-c", "core.fsmonitor=false", "diff", "--binary", "HEAD"], capture=True)
    (LOCAL / "build-evidence.json").write_text(
        json.dumps(
            {
                "source_commit": source,
                "working_diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
                "tag": args.tag,
                "ml_base": ml_base,
                "images": images,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    ecr.close()
    print("Published C3 images and saved digest manifest")


if __name__ == "__main__":
    main()
