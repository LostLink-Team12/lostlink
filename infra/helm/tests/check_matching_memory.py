"""Check both Matching imports/inference at the chart's default memory limit.

The containers use no AWS, database, Kubernetes, or network. This checks model
load/preprocessing peaks, not live concurrency or cloud node capacity.
"""

import argparse
import json
import re
import subprocess
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def memory_bytes(value):
    match = re.fullmatch(r"([0-9]+)(Mi|Gi)", str(value))
    if not match:
        raise ValueError("Memory must use integer Mi or Gi")
    return int(match[1]) * (1024 ** (2 if match[2] == "Mi" else 3))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args()
    values = yaml.safe_load((ROOT / "infra/helm/lostlink/values.yaml").read_text())
    matching = values["services"]["matching"]
    probe = Path(__file__).with_name("matching_memory_probe.py").read_text()
    for mode, process in [("api", matching["api"]), ("tasks", matching["workers"]["tasks"])]:
        limit = memory_bytes(process["resources"]["limits"]["memory"])
        name = "lostlink-memory-" + uuid.uuid4().hex[:12]
        command = [
            "docker",
            "run",
            "--name",
            name,
            "--interactive",
            "--network",
            "none",
            "--memory",
            str(limit),
            "--memory-swap",
            str(limit),
            "--cpus",
            "1",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m",
            "--mount",
            f"type=bind,src={args.model_dir.resolve()},dst=/models,readonly",
            "--env",
            "ENVIRONMENT=local",
            "--env",
            f"PROBE_MODE={mode}",
            "--env",
            "CLIP_CHECKPOINT=/models/ViT-B-32.pt",
            "--env",
            f"CLIP_SHA256={values['matching']['model']['sha256']}",
            args.image,
            "python",
            "-",
        ]
        try:
            result = subprocess.run(command, input=probe, text=True, timeout=300, check=False)
            state = json.loads(subprocess.check_output(["docker", "inspect", name], text=True))[0]
            oom = state["State"]["OOMKilled"]
            print(json.dumps({"process": mode, "limit_bytes": limit, "oom_killed": oom}))
            if result.returncode or oom:
                raise RuntimeError(f"Matching {mode} memory smoke failed: exit {result.returncode}")
        finally:
            # Remove only the unique container created by this invocation.
            subprocess.run(["docker", "rm", "--force", name], capture_output=True, check=False)


if __name__ == "__main__":
    main()
