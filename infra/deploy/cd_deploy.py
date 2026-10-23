"""Deploy CI-published digests with named Deployment/ConfigMap patch permissions."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ("venue", "item", "matching", "claim", "notification")
MODES = {
    "venue": ("api", "outbox", "identity"),
    "item": ("api", "tasks", "outbox", "consume", "cleanup"),
    "matching": ("api", "tasks", "consume", "outbox"),
    "claim": ("api", "tasks", "outbox"),
    "notification": ("api", "consume", "tasks"),
}
TARGETS = {f"{s}-{m}": (s, "app") for s in SERVICES for m in MODES[s]}
TARGETS.update(frontend=("frontend", "gateway"), **{"embedding-metrics": ("venue", "exporter")})
STATE = "lostlink-cd-state"


def run(command):
    value = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
    if value.returncode:
        raise RuntimeError("Deployment command failed: " + " ".join(command[:2]))
    return value.stdout


def migration_hashes(ref):
    result = {}
    for service in SERVICES:
        tree = run(["git", "ls-tree", "-r", ref, "--", f"services/{service}-service/migrations"])
        if not tree:
            raise RuntimeError("Missing migration source: " + service)
        result[service] = hashlib.sha256(tree.encode()).hexdigest()
    return result


def require_migrated(state, proposed):
    if state.get("migration_hashes") != proposed:
        raise RuntimeError(
            "Database migrations changed; run operator migrations and approve the baseline"
        )


def resolve_images(ecr, account, region, sha):
    images = {}
    for service in (*SERVICES, "frontend"):
        repo = "lostlink/" + (service if service == "frontend" else service + "-service")
        value = ecr.describe_images(repositoryName=repo, imageIds=[{"imageTag": sha}])
        digest = value["imageDetails"][0]["imageDigest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RuntimeError("Invalid published image digest")
        images[service] = f"{account}.dkr.ecr.{region}.amazonaws.com/{repo}@{digest}"
    return images


def pod_patch(deployment, container, image=None, sha=None, revision=None):
    template = deployment["spec"]["template"]
    original = next((c for c in template["spec"]["containers"] if c["name"] == container), None)
    if original is None:
        raise RuntimeError("Expected application container is missing")
    previous = template.get("metadata", {}).get("annotations", {})
    annotations = {"lostlink/source-commit": sha, "lostlink/runtime-revision": revision}
    if image is None:
        image = original["image"]
        annotations = {key: previous.get(key) for key in annotations}
    return {
        "spec": {
            "template": {
                "metadata": {"annotations": annotations},
                "spec": {"containers": [{"name": container, "image": image}]},
            }
        }
    }


def patch(kube, kind, name, value, strategy="merge"):
    run([*kube, "patch", kind, name, "--type=" + strategy, "-p", json.dumps(value)])


def deploy(args, kube, state, images):
    require_migrated(state, migration_hashes(args.sha))
    runtime = {
        name: (ROOT / "infra/deploy" / name).read_text(encoding="utf-8")
        for name in ("runtime.py", "worker-probe.py", "backlog.py")
    }
    runtime["prepare_clip_model.py"] = (ROOT / "scripts/prepare_clip_model.py").read_text(
        encoding="utf-8"
    )
    revision = hashlib.sha256(json.dumps(runtime, sort_keys=True).encode()).hexdigest()
    previous = json.loads(run([*kube, "get", "configmap", "lostlink-runtime", "-o", "json"]))
    originals = {
        d["metadata"]["name"]: d
        for d in json.loads(run([*kube, "get", "deployments", "-o", "json"]))["items"]
    }
    if not set(TARGETS).issubset(originals):
        raise RuntimeError("CD requires the complete existing C3 deployment")
    changes = {
        name: pod_patch(originals[name], container, images[service], args.sha, revision)
        for name, (service, container) in TARGETS.items()
    }
    changed = []
    try:
        patch(kube, "configmap", "lostlink-runtime", {"data": runtime})
        for name, value in changes.items():
            changed.append(name)
            patch(kube, "deployment", name, value, "strategic")
        for name in changes:
            run([*kube, "rollout", "status", "deployment/" + name, "--timeout=900s"])
            print("Ready: " + name, flush=True)
        run([sys.executable, "infra/deploy/smoke.py", "--origin", state["origin"]])
        patch(
            kube,
            "configmap",
            STATE,
            {
                "data": {
                    "state.json": json.dumps(
                        {
                            **state,
                            "source_commit": args.sha,
                            "images": images,
                        }
                    )
                }
            },
        )
    except Exception:
        failures = []
        try:
            patch(kube, "configmap", "lostlink-runtime", {"data": previous["data"]})
        except RuntimeError:
            failures.append("runtime")
        for name in reversed(changed):
            try:
                patch(
                    kube,
                    "deployment",
                    name,
                    pod_patch(originals[name], TARGETS[name][1]),
                    "strategic",
                )
            except RuntimeError:
                failures.append(name)
        for name in changed:
            try:
                run([*kube, "rollout", "status", "deployment/" + name, "--timeout=900s"])
            except RuntimeError:
                failures.append(name)
        if failures:
            raise RuntimeError("Operator rollback required: " + ", ".join(failures)) from None
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--kubeconfig", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha) or not re.fullmatch(r"[0-9]{12}", args.account):
        raise ValueError("Require a full source SHA and account ID")
    if run(["git", "rev-parse", "HEAD"]).strip() != args.sha:
        raise RuntimeError("Checkout must match the CI-published source SHA")
    kube = [
        args.kubectl,
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        "lostlink-dev",
        "-n",
        "lostlink",
    ]
    session = boto3.Session(region_name=args.region)
    config = Config(connect_timeout=5, read_timeout=30, retries={"mode": "standard"})
    with session.client("sts", config=config) as sts:
        if sts.get_caller_identity()["Account"] != args.account:
            raise RuntimeError("Wrong deployment account")
    with session.client("eks", config=config) as eks:
        endpoint = eks.describe_cluster(name="lostlink-dev")["cluster"]["endpoint"]
        current = json.loads(run([*kube, "config", "view", "--minify", "-o", "json"]))
        if current["clusters"][0]["cluster"]["server"] != endpoint:
            raise RuntimeError("Wrong Kubernetes endpoint")
    value = json.loads(run([*kube, "get", "configmap", STATE, "-o", "json"]))
    state = json.loads(value["data"]["state.json"])
    with session.client("ecr", config=config) as ecr:
        images = resolve_images(ecr, args.account, args.region, args.sha)
    deploy(args, kube, state, images)
    print("Dev release and HTTPS checks passed: " + args.sha)


if __name__ == "__main__":
    main()
