"""Approve a migration baseline only after successful isolated operator migrations."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import cd_migrate as cm
import pytest


@pytest.mark.parametrize("failure", [None, "migration", "concurrent", "images"])
def test_operator_migration_keeps_release_state_and_never_approves_failure(monkeypatch, failure):
    state = {
        "source_commit": "a" * 40,
        "images": {"claim": "old@sha256:digest"},
        "origin": "https://offline.invalid",
        "migration_hashes": {"claim": "old", "item": "same"},
    }
    before = {"metadata": {"resourceVersion": "7"}, "data": {"state.json": json.dumps(state)}}
    after = copy.deepcopy(before)
    if failure == "concurrent":
        after["metadata"]["resourceVersion"] = "8"
    cluster = Mock(command=["kubectl", "--context", "lostlink-dev"])
    cluster.get.side_effect = [before, after]
    proposed = {"claim": "new", "item": "same"}
    monkeypatch.setattr(cm, "migration_hashes", lambda _: proposed)
    client = Mock()
    monkeypatch.setattr(
        cm,
        "resolve_images",
        Mock(side_effect=RuntimeError("images unavailable") if failure == "images" else None),
    )
    commands = []

    def command(args):
        commands.append(args)
        if args[:2] == ["git", "rev-parse"]:
            return "b" * 40
        if "--image-sha" in args and failure == "migration":
            raise RuntimeError("migration failed")
        return ""

    monkeypatch.setattr(cm, "run", command)
    args = SimpleNamespace(
        sha="b" * 40,
        profile="offline",
        region="ap-southeast-1",
        kubectl="kubectl",
        kubeconfig="offline-config",
    )
    session = Mock()
    session.client.return_value = client
    data = {"application": {"value": {"kms_key_arn": "arn:aws:kms:r:123456789012:key/k"}}}
    if failure:
        with pytest.raises(RuntimeError):
            cm.migrate(args, cluster, data, session)
        assert not any("patch" in c for c in commands)
    else:
        cm.migrate(args, cluster, data, session)
        migration = next(c for c in commands if "--image-sha" in c)
        assert (
            migration[-1] == args.sha and migration[migration.index("--profile") + 1] == "offline"
        )
        patch = json.loads(next(c for c in commands if "patch" in c)[-1])
        assert patch[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "7"}
        result = json.loads(patch[1]["value"])
        assert result == {
            **state,
            "migration_hashes": proposed,
            "migration_source_commit": args.sha,
        }
    client.close.assert_called_once_with()
    assert json.loads(before["data"]["state.json"]) == state


def test_wrong_checkout_cannot_start_migrations(monkeypatch):
    monkeypatch.setattr(cm, "run", lambda _: "a" * 40)
    cluster = Mock()
    with pytest.raises(RuntimeError, match="Checkout must match"):
        cm.migrate(SimpleNamespace(sha="b" * 40), cluster, {}, Mock())
    cluster.validate.assert_not_called()
