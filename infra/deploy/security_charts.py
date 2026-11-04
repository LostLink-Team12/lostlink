"""Render both supported chart layouts with synthetic CI inputs, without AWS."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ci_values import service_values

ROOT = Path(__file__).resolve().parents[2]


def render(destination, helm):
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "infra/deploy/cd-rbac.yaml", destination / "cd-rbac.yaml")
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        repositories = work / "repositories.yaml"
        repositories.write_text("apiVersion: v1\nrepositories: []\n", encoding="utf-8")
        env = {
            **os.environ,
            "HELM_REPOSITORY_CONFIG": str(repositories),
            "HELM_REPOSITORY_CACHE": str(work / "cache"),
        }

        def template(name, chart, args):
            result = subprocess.run(
                [helm, "template", name, str(ROOT / "infra/helm" / chart), "-n", "lostlink", *args],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            (destination / f"{name}.yaml").write_text(result.stdout, encoding="utf-8")

        for name, values in service_values().items():
            chart = ROOT / "infra/helm" / (name + "-service")
            subprocess.run(
                [helm, "dependency", "build", str(chart), "--skip-refresh"],
                check=True,
                capture_output=True,
                env=env,
            )
            values["migration"] = {"enabled": True, "existingSecret": "ci-migration-placeholder"}
            value_path = work / f"{name}.json"
            value_path.write_text(json.dumps(values), encoding="utf-8")
            template(name, name + "-service", ["-f", str(value_path)])

        platform = work / "platform.json"
        platform.write_text(
            json.dumps(
                {
                    "frontendImage": "example/frontend@sha256:" + "a" * 64,
                    "metricsImage": "example/venue@sha256:" + "b" * 64,
                    "albSecurityGroup": "sg-ci-placeholder",
                    "subnets": ["subnet-ci-placeholder"],
                    "originToken": "ci-rendering-placeholder",
                }
            ),
            encoding="utf-8",
        )
        template("platform", "platform", ["-f", str(platform)])
        for name, extra in (
            ("umbrella", []),
            ("umbrella-migrations", ["--set", "migrations.enabled=true"]),
        ):
            template(
                name,
                "lostlink",
                [
                    "-f",
                    str(ROOT / "infra/helm/tests/values-ci.yaml"),
                    *extra,
                ],
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--helm", default=os.environ.get("HELM_BIN", "helm"))
    args = parser.parse_args()
    render(args.destination, args.helm)
