"""CD must preserve operator settings, reject unsafe releases and undo failures."""

import copy
import json
from types import SimpleNamespace

import cd_access
import cd_deploy as cd
import pytest
import yaml


def workload(name="item-api", container="app"):
    return {
        "metadata": {"name": name},
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {
                    "annotations": {"operator": "keep", "lostlink/runtime-revision": "old"}
                },
                "spec": {
                    "serviceAccountName": name,
                    "containers": [
                        {
                            "name": container,
                            "image": "old@sha256:x",
                            "resources": {"requests": {"memory": "4Gi"}},
                            "env": [{"name": "PRIVATE_ARN", "value": "keep"}],
                        }
                    ],
                },
            },
        },
    }


def test_release_patch_changes_only_image_and_revision_and_rollback_removes_new_marker():
    original = workload()
    before = copy.deepcopy(original)
    patch = cd.pod_patch(original, "app", "repo@sha256:new", "a" * 40, "new")
    assert original == before
    assert set(patch["spec"]) == {"template"}
    assert patch["spec"]["template"]["spec"] == {
        "containers": [{"name": "app", "image": "repo@sha256:new"}]
    }
    restore = cd.pod_patch(original, "app")["spec"]["template"]
    assert restore["metadata"]["annotations"]["lostlink/source-commit"] is None
    assert restore["metadata"]["annotations"]["lostlink/runtime-revision"] == "old"


def test_missing_container_and_unapproved_migrations_fail_closed():
    with pytest.raises(RuntimeError, match="container"):
        cd.pod_patch(workload(), "wrong", "new")
    with pytest.raises(RuntimeError, match="migrations changed"):
        cd.require_migrated({"migration_hashes": {"item": "old"}}, {"item": "new"})


def test_failed_smoke_restores_runtime_and_all_workloads_without_advancing_state(monkeypatch):
    originals = [workload(n, c) for n, (_, c) in cd.TARGETS.items()]
    calls = []

    def command(args):
        if "deployments" in args:
            return json.dumps({"items": originals})
        if "lostlink-runtime" in args:
            return json.dumps({"data": {"runtime.py": "old"}})
        if "infra/deploy/smoke.py" in args:
            raise RuntimeError("injected smoke failure")
        return ""

    monkeypatch.setattr(cd, "run", command)
    monkeypatch.setattr(cd, "patch", lambda *args: calls.append(args))
    monkeypatch.setattr(cd, "migration_hashes", lambda _: {})
    with pytest.raises(RuntimeError, match="injected smoke failure"):
        cd.deploy(
            SimpleNamespace(sha="a" * 40),
            [],
            {"migration_hashes": {}, "origin": "https://example.com"},
            {s: "new" for s in (*cd.SERVICES, "frontend")},
        )
    assert all(c[2] != cd.STATE for c in calls)
    assert len([c for c in calls if c[1] == "deployment"]) == 2 * len(cd.TARGETS)
    assert calls[len(cd.TARGETS) + 1][3] == {"data": {"runtime.py": "old"}}


def test_rbac_has_no_secrets_rbac_exec_or_create_and_exact_patch_targets():
    role = next(yaml.safe_load_all((cd.ROOT / "infra/deploy/cd-rbac.yaml").read_text()))
    targets = set()
    for rule in role["rules"]:
        assert not set(rule["resources"]) & {
            "secrets",
            "roles",
            "rolebindings",
            "pods/exec",
            "jobs",
        }
        assert not set(rule["verbs"]) & {"create", "update", "delete", "*"}
        if "patch" in rule["verbs"] and "deployments" in rule["resources"]:
            targets.update(rule["resourceNames"])
    assert targets == set(cd.TARGETS)


def test_workflow_gates_deploy_on_publish_and_never_deploys_prs():
    flow = yaml.safe_load((cd.ROOT / ".github/workflows/ci.yml").read_text())
    jobs = flow["jobs"]
    assert "publish-image" in jobs["deploy-dev"]["needs"]
    assert (
        "pull_request" in jobs["deploy-dev"]["if"] and "refs/heads/main" in jobs["deploy-dev"]["if"]
    )
    assert jobs["deploy-dev"]["concurrency"]["cancel-in-progress"] is False
    cleanup = next(
        s for s in jobs["deploy-dev"]["steps"] if s.get("name") == "Remove runner endpoint access"
    )
    assert "always()" in cleanup["if"]
    assert "build-frontend" in jobs["publish-image"]["needs"]


class Endpoint:
    def __init__(self, cidrs):
        self.cidrs = cidrs

    def describe_cluster(self, **_):
        return {
            "cluster": {
                "status": "ACTIVE",
                "resourcesVpcConfig": {
                    "endpointPublicAccess": True,
                    "endpointPrivateAccess": True,
                    "publicAccessCidrs": self.cidrs,
                },
            }
        }


def test_lease_cleanup_preserves_other_operator_changes(monkeypatch, tmp_path):
    client = Endpoint(["1.1.1.1/32"])

    def change(_, cluster, cidrs):
        assert cluster == "lostlink-dev"
        client.cidrs = cidrs

    monkeypatch.setattr(cd_access, "change", change)
    path = tmp_path / "lease.json"
    cd_access.open_access(client, "lostlink-dev", "8.8.8.8", path)
    assert client.cidrs == ["1.1.1.1/32", "8.8.8.8/32"]
    client.cidrs.append("9.9.9.9/32")
    cd_access.close_access(client, path)
    assert client.cidrs == ["1.1.1.1/32", "9.9.9.9/32"] and not path.exists()


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "0.0.0.0", "::1"])
def test_lease_rejects_non_public_ipv4(address, tmp_path):
    with pytest.raises(ValueError):
        cd_access.open_access(Endpoint([]), "lostlink-dev", address, tmp_path / "lease.json")


def test_already_allowed_runner_is_not_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(cd_access, "change", lambda *_: pytest.fail("Unexpected allowlist update"))
    path = tmp_path / "lease.json"
    client = Endpoint(["8.8.8.0/24"])
    cd_access.open_access(client, "lostlink-dev", "8.8.8.8", path)
    cd_access.close_access(client, path)
    assert client.cidrs == ["8.8.8.0/24"]
