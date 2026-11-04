import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


probe = module("dast_probe", "tests/security/dast/probe.py")
host = module("dast_host", "scripts/dast.py")


@pytest.mark.parametrize("path", ["//outside/", "@outside/", "https://outside/", "/#fragment"])
def test_candidate_traffic_cannot_escape_to_other_origins(path):
    with pytest.raises(ValueError):
        probe.check_path(path)


def test_dast_blocks_medium_and_high_and_redacts_response_content():
    safe = probe.alerts_verdict(
        [
            {
                "riskCode": "2",
                "pluginId": "10038",
                "url": "https://frontend:8443/config.json?secret=private",
                "evidence": "private",
                "param": "private",
                "other": "private",
            }
        ]
    )
    assert safe[0]["blocking"]
    assert "private" not in json.dumps(safe)
    with pytest.raises(ValueError):
        probe.alerts_verdict(None)
    with pytest.raises(ValueError):
        probe.alerts_verdict([{"riskCode": "3", "url": "https://outside/"}])


def test_private_stack_has_no_published_ports_or_cloud_identity(tmp_path):
    config = host.configuration(tmp_path / "private", tmp_path / "public", "dast-test")
    assert config["networks"]["default"]["internal"] is True
    for service in config["services"].values():
        assert not service.get("ports")
        assert not service.get("privileged")
        assert all("docker.sock" not in str(v) for v in service.get("volumes", []))
    assert config["services"]["item"]["environment"]["ENVIRONMENT"] == "test"
    assert "lostlink_item_api:" in config["services"]["item"]["environment"]["DATABASE_URL"]


def test_required_dast_is_candidate_code_with_no_skipping_condition():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    assert "dast-pr" in jobs["security-gate"]["needs"]
    assert not jobs["dast-pr"].get("if")
    assert not jobs["dast-pr"].get("permissions", {}).get("id-token")
    assert any("--sha" in s.get("run", "") for s in jobs["dast-pr"]["steps"])


def test_cleanup_refuses_unrelated_project(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "state.json").write_text(json.dumps({"project": "production"}))
    with pytest.raises(ValueError, match="unrelated"):
        host.cleanup(tmp_path)
