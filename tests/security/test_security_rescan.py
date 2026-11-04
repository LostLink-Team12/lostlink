import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "deployment_manifest", ROOT / "scripts/deployment_manifest.py"
)
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)


def record():
    return {
        "source_commit": "a" * 40,
        "images": {
            s: "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/lostlink/"
            + (s if s == "frontend" else s + "-service")
            + "@sha256:"
            + "b" * 64
            for s in manifest.SERVICES
        },
    }


def test_deployment_scan_uses_exact_digest_and_source():
    assert len(manifest.validate(record(), "123456789012", "ap-southeast-1", "a" * 40)) == 6
    for change in ("tag", "registry", "incomplete", "source"):
        data = record()
        if change == "tag":
            data["images"]["frontend"] = data["images"]["frontend"].split("@")[0] + ":latest"
        elif change == "registry":
            data["images"]["frontend"] = data["images"]["frontend"].replace(
                "123456789012", "999999999999"
            )
        elif change == "incomplete":
            del data["images"]["frontend"]
        else:
            data["source_commit"] = "c" * 40
        with pytest.raises(ValueError):
            manifest.validate(data, "123456789012", "ap-southeast-1", "a" * 40)


def test_artifact_selection_rejects_expired_foreign_and_pr_records():
    workflow = yaml.safe_load((ROOT / ".github/workflows/security-rescan.yml").read_text())
    step = next(
        s for s in workflow["jobs"]["deployed-manifest"]["steps"] if s.get("id") == "latest"
    )
    script = step["with"]["script"]
    artifacts = [
        {
            "id": 4,
            "expired": False,
            "workflow_run": {
                "id": 20,
                "head_branch": "main",
                "repository_id": 10,
                "head_sha": "a" * 40,
            },
        }
    ]
    for index, changes in enumerate(
        (
            {"expired": True},
            {"workflow_run": {"head_branch": "feature", "repository_id": 10}},
            {"workflow_run": {"head_branch": "main", "repository_id": 99}},
        ),
        start=5,
    ):
        artifacts.append({**artifacts[0], "id": index, **changes})
    runner = """
    const fs = require('fs');
    const p = JSON.parse(fs.readFileSync(0,'utf8'));
    const outputs = {};
    const github = {rest:{repos:{get:async()=>({data:{id:10}})},actions:{listArtifactsForRepo(){}}},
      paginate:async()=>p.artifacts};
    const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
    new AsyncFunction('github','context','core',p.script)(github,{repo:{owner:'test',repo:'test'}},
      {setOutput:(k,v)=>{outputs[k]=v}}).then(()=>{process.stdout.write(JSON.stringify(outputs))})
      .catch(()=>process.exit(1));
    """
    result = subprocess.run(
        ["node", "-e", runner],
        input=json.dumps({"script": script, "artifacts": artifacts}),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"id": 4, "run": 20, "sha": "a" * 40}


def test_scheduled_and_manual_scans_never_invoke_deployment_or_publishing():
    scheduled = yaml.safe_load((ROOT / ".github/workflows/security-rescan.yml").read_text())
    assert not {"deploy-dev", "publish-image"} & set(scheduled["jobs"])
    assert scheduled["jobs"]["sast"]["uses"] == "./.github/workflows/sast.yml"
    assert scheduled["jobs"]["dast"]["uses"] == "./.github/workflows/dast.yml"
    dast = yaml.safe_load((ROOT / ".github/workflows/dast.yml").read_text())
    assert "id-token" not in dast["permissions"]
