"""A zero replica target alone does not prove the email sender has stopped."""

import json
from types import SimpleNamespace

import acceptance
import pytest


@pytest.mark.parametrize(
    "pod",
    [
        {"metadata": {}, "status": {"phase": "Running"}},
        {"metadata": {"deletionTimestamp": "2026-10-23T00:00:00Z"}, "status": {"phase": "Running"}},
    ],
)
def test_zero_target_rejects_live_or_terminating_sender(monkeypatch, pod):
    cluster = SimpleNamespace(command=["kubectl"], get=lambda *_: {"spec": {"replicas": 0}})
    monkeypatch.setattr(acceptance, "run", lambda _: json.dumps({"items": [pod]}))
    with pytest.raises(RuntimeError, match="fully exited"):
        acceptance.require_paused_sender(cluster)


@pytest.mark.parametrize("deployment", [None, {"spec": {"replicas": 1}}])
def test_unpaused_or_missing_deployment_is_rejected_before_pod_lookup(monkeypatch, deployment):
    cluster = SimpleNamespace(command=["kubectl"], get=lambda *_: deployment)

    def unexpected(_):
        raise AssertionError("Should reject the sender before listing pods")

    monkeypatch.setattr(acceptance, "run", unexpected)
    with pytest.raises(RuntimeError, match="Pause"):
        acceptance.require_paused_sender(cluster)


def test_paused_sender_without_pods_can_run(monkeypatch):
    cluster = SimpleNamespace(command=["kubectl"], get=lambda *_: {"spec": {"replicas": 0}})
    monkeypatch.setattr(acceptance, "run", lambda _: '{"items": []}')
    acceptance.require_paused_sender(cluster)
