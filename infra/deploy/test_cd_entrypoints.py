"""Exercise CD CLI boundaries with real SDK clients and no AWS credentials/network."""

import io
import json
import socket
import sys
from contextlib import ExitStack
from unittest.mock import Mock

import boto3
import cd_access
import cd_deploy as cd
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

ACCOUNT = "123456789012"
SHA = "b" * 40
ENDPOINT = "https://eks.offline.invalid"


@pytest.fixture
def sdk(monkeypatch):
    # Explicit dummy credentials prevent profile/IMDS credential lookup.
    session = boto3.Session(
        aws_access_key_id="offline",
        aws_secret_access_key="offline",
        region_name="ap-southeast-1",
    )
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("Unexpected network"))
    with ExitStack() as stack:
        clients = {}
        stubs = {}
        for service in ("sts", "eks", "ecr"):
            client = session.client(service)
            assert not hasattr(client, "__enter__")
            monkeypatch.setattr(client, "close", Mock(wraps=client.close))
            stack.callback(client.close)
            clients[service] = client
            stubs[service] = stack.enter_context(Stubber(client))
        yield clients, stubs


def test_cleanup_without_lease_needs_no_aws_client(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["cd_access.py", "close", "--lease", str(tmp_path / "absent")])
    monkeypatch.setattr(
        cd_access.boto3, "client", lambda *_a, **_k: pytest.fail("AWS client created")
    )
    monkeypatch.setattr(cd_access, "urlopen", lambda *_a, **_k: pytest.fail("Network attempted"))
    cd_access.main()


@pytest.mark.parametrize("action", ["open", "close"])
@pytest.mark.parametrize("failure", [False, True])
def test_access_cli_closes_real_eks_client(sdk, monkeypatch, tmp_path, action, failure):
    clients, stubs = sdk
    lease = tmp_path / "lease.json"
    if action == "close":
        lease.write_text(
            json.dumps({"cluster": "lostlink-dev", "cidr": "8.8.8.8/32", "added": True})
        )
    monkeypatch.setattr(sys, "argv", ["cd_access.py", action, "--lease", str(lease)])
    monkeypatch.setattr(cd_access.boto3, "client", lambda *_a, **_k: clients["eks"])
    monkeypatch.setattr(cd_access, "urlopen", lambda *_a, **_k: io.BytesIO(b"8.8.8.8\n"))
    if failure:
        stubs["eks"].add_client_error(
            "describe_cluster", service_error_code="AccessDeniedException"
        )
        with pytest.raises(ClientError):
            cd_access.main()
    else:
        # Open: already allowed. Close: already removed by another operator.
        stubs["eks"].add_response(
            "describe_cluster",
            {
                "cluster": {
                    "status": "ACTIVE",
                    "resourcesVpcConfig": {
                        "endpointPublicAccess": True,
                        "endpointPrivateAccess": True,
                        "publicAccessCidrs": ["8.8.8.0/24"] if action == "open" else ["1.1.1.1/32"],
                    },
                }
            },
            {"name": "lostlink-dev"},
        )
        cd_access.main()
        assert lease.exists() is (action == "open")
    clients["eks"].close.assert_called_once_with()
    stubs["eks"].assert_no_pending_responses()


@pytest.mark.parametrize("failure", [None, "account", "endpoint", "images", "deploy"])
def test_release_cli_closes_clients_and_preserves_guards(sdk, monkeypatch, failure):
    clients, stubs = sdk
    created = []

    def client(service, **_):
        created.append(service)
        return clients[service]

    monkeypatch.setattr(cd.boto3, "Session", lambda **_: Mock(client=client))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cd_deploy.py",
            "--sha",
            SHA,
            "--account",
            ACCOUNT,
            "--kubeconfig",
            "offline-config",
        ],
    )
    state = {"origin": "https://app.offline.invalid", "migration_hashes": {}}
    commands = []

    def command(args):
        commands.append(args)
        if args[:2] == ["git", "rev-parse"]:
            return SHA
        if "config" in args:
            return json.dumps(
                {
                    "clusters": [
                        {
                            "cluster": {
                                "server": "https://wrong.invalid"
                                if failure == "endpoint"
                                else ENDPOINT,
                            }
                        }
                    ]
                }
            )
        assert args[-5:] == ["get", "configmap", cd.STATE, "-o", "json"]
        return json.dumps({"data": {"state.json": json.dumps(state)}})

    monkeypatch.setattr(cd, "run", command)
    stubs["sts"].add_response(
        "get_caller_identity", {"Account": "999999999999" if failure == "account" else ACCOUNT}
    )
    if failure != "account":
        stubs["eks"].add_response(
            "describe_cluster", {"cluster": {"endpoint": ENDPOINT}}, {"name": "lostlink-dev"}
        )
    if failure not in {"account", "endpoint"}:
        for service in (*cd.SERVICES, "frontend"):
            params = {
                "repositoryName": "lostlink/"
                + (service if service == "frontend" else service + "-service"),
                "imageIds": [{"imageTag": SHA}],
            }
            if failure == "images":
                stubs["ecr"].add_client_error(
                    "describe_images",
                    service_error_code="ImageNotFoundException",
                    expected_params=params,
                )
                break
            stubs["ecr"].add_response(
                "describe_images", {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64}]}, params
            )
    deploy = Mock(
        side_effect=RuntimeError("injected rollout failure") if failure == "deploy" else None
    )
    monkeypatch.setattr(cd, "deploy", deploy)
    if failure:
        errors = {
            "account": "Wrong deployment account",
            "endpoint": "Wrong Kubernetes endpoint",
            "images": "ImageNotFoundException",
            "deploy": "injected rollout failure",
        }
        with pytest.raises((RuntimeError, ClientError), match=errors[failure]):
            cd.main()
    else:
        cd.main()
    expected = (
        ["sts"]
        if failure == "account"
        else ["sts", "eks"]
        if failure == "endpoint"
        else ["sts", "eks", "ecr"]
    )
    assert created == expected
    for service, instance in clients.items():
        assert instance.close.call_count == int(service in expected)
        stubs[service].assert_no_pending_responses()
    if failure in {"account", "endpoint", "images"}:
        deploy.assert_not_called()
        assert not any("patch" in c for c in commands)
    else:
        deploy.assert_called_once()
        args, kube, actual_state, images = deploy.call_args.args
        assert args.sha == SHA and actual_state == state
        assert kube == [
            "kubectl",
            "--kubeconfig",
            "offline-config",
            "--context",
            "lostlink-dev",
            "-n",
            "lostlink",
        ]
        assert set(images) == {*cd.SERVICES, "frontend"}
        assert all("@sha256:" in image for image in images.values())
