"""Security gates must reject bad/incomplete evidence without leaking raw secrets."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("trivy_gate", ROOT / "scripts/trivy_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def report(*results):
    return {"SchemaVersion": 2, "Results": list(results)}


def image():
    return {
        "Target": "test-image (alpine 3.22)",
        "Type": "alpine",
        "Class": "os-pkgs",
        "Packages": [{"Name": "musl", "Version": "1.2.5"}],
    }


@pytest.mark.parametrize(
    ("severity", "fixed", "blocked"),
    [("CRITICAL", "2.0", 1), ("HIGH", "2.0", 1), ("HIGH", "", 0), ("MEDIUM", "2.0", 0)],
)
def test_fixable_high_vulnerabilities_block_but_full_report_retains_other_findings(
    severity, fixed, blocked
):
    result = image()
    result["Vulnerabilities"] = [
        {
            "VulnerabilityID": "CVE-TEST",
            "Severity": severity,
            "FixedVersion": fixed,
        }
    ]
    evaluated = gate.evaluate(report(result), "frontend-image")
    assert evaluated["BlockingFindings"] == blocked
    assert len(evaluated["Results"][0]["Vulnerabilities"]) == 1


@pytest.mark.parametrize(("status", "blocked"), [("FAIL", 1), ("PASS", 0), ("EXCEPTION", 0)])
def test_only_failed_high_misconfigurations_block(status, blocked):
    result = image()
    result["Misconfigurations"] = [{"ID": "TEST", "Severity": "HIGH", "Status": status}]
    assert gate.evaluate(report(result), "frontend-image")["BlockingFindings"] == blocked


def test_secret_and_source_context_never_reach_public_evidence():
    canary = "private_content_must_never_be_published"
    result = image()
    result["Secrets"] = [
        {
            "RuleID": "test-rule",
            "Severity": "LOW",
            "StartLine": 9,
            "Match": canary,
            "Code": {"Lines": [{"Content": canary}]},
        }
    ]
    result["Misconfigurations"] = [
        {
            "ID": "TEST",
            "Severity": "LOW",
            "Status": "FAIL",
            "CauseMetadata": {"Code": canary},
        }
    ]
    result["MisconfSummary"] = {"Failures": 1, "Code": canary}
    evaluated = gate.evaluate(report(result), "frontend-image")
    assert evaluated["BlockingFindings"] == 1
    assert canary not in json.dumps(evaluated)
    assert evaluated["Results"][0]["Secrets"][0]["StartLine"] == 9


@pytest.mark.parametrize("bad_report", [{}, {"SchemaVersion": 2}, report(), report({})])
def test_absent_or_malformed_scan_results_fail_closed(bad_report):
    with pytest.raises(ValueError):
        gate.evaluate(bad_report, "frontend-image")


def test_unknown_finding_severity_fails_closed():
    result = image()
    result["Vulnerabilities"] = [{"Severity": "NOT_A_SEVERITY", "FixedVersion": "2"}]
    with pytest.raises(ValueError, match="severity"):
        gate.evaluate(report(result), "frontend-image")


def test_empty_image_package_inventory_cannot_pass():
    result = image()
    result["Packages"] = []
    with pytest.raises(gate.CoverageError, match="OS package"):
        gate.evaluate(report(result), "frontend-image")


def test_matching_image_requires_real_ml_inventory():
    result = image()
    result["Packages"] += [
        {"Name": "fastapi", "Version": "1"},
        {"Name": "starlette", "Version": "1"},
    ]
    with pytest.raises(gate.CoverageError, match="Python runtime"):
        gate.evaluate(report(result), "matching-image")
    result["Packages"].append({"Name": "torch", "Version": "1"})
    assert gate.evaluate(report(result), "matching-image")["BlockingFindings"] == 0


def test_all_repository_lock_packages_including_dev_and_ml_are_required(tmp_path):
    results = []
    for name in gate.LOCKS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith("package-lock.json"):
            path.write_text(
                json.dumps(
                    {
                        "packages": {
                            "": {},
                            "node_modules/test": {"version": "1.0", "dev": True},
                        }
                    }
                ),
                encoding="utf-8",
            )
        else:
            path.write_text("test-package==1.0\n", encoding="utf-8")
        results.append(
            {
                "Target": name,
                "Packages": [
                    {
                        "Name": "test" if name.endswith(".json") else "test_package",
                        "Version": "1.0",
                    }
                ],
            }
        )
    for kind in ("terraform", "dockerfile"):
        results.append({"Target": kind, "Type": kind, "MisconfSummary": {"Successes": 1}})
    assert len(gate.evaluate(report(*results), "repository", root=tmp_path)["Coverage"]) == 5
    # Losing any one manifest or one pinned package must reject the report.
    for index in range(5):
        broken = json.loads(json.dumps(results))
        broken[index]["Packages"] = []
        with pytest.raises(gate.CoverageError, match="Incomplete package"):
            gate.evaluate(report(*broken), "repository", root=tmp_path)


def test_every_rendered_manifest_must_have_actual_scan_results(tmp_path):
    results = []
    for name in gate.RENDERED_MANIFESTS:
        (tmp_path / name).write_text("kind: Deployment\n", encoding="utf-8")
        if name != "platform.yaml":
            results.append(
                {
                    "Target": name,
                    "Type": "kubernetes",
                    "MisconfSummary": {"Successes": 1},
                }
            )
    with pytest.raises(gate.CoverageError, match="platform.yaml"):
        gate.evaluate(report(*results), "kubernetes", scan_ref=tmp_path)
    results.append({**results[0], "Target": "platform.yaml"})
    assert len(gate.evaluate(report(*results), "kubernetes", scan_ref=tmp_path)["Coverage"]) == 9
    (tmp_path / "venue.yaml").unlink()
    with pytest.raises(gate.CoverageError, match="Missing rendered manifests: venue.yaml"):
        gate.evaluate(report(*results), "kubernetes", scan_ref=tmp_path)


@pytest.mark.parametrize("evidence", [None, "not-json-with-private-content", "{}"])
def test_cli_writes_safe_failure_report_for_missing_or_invalid_input(tmp_path, evidence):
    raw = tmp_path / "raw.json"
    if evidence is not None:
        raw.write_text(evidence, encoding="utf-8")
    destination = tmp_path / "public"
    assert (
        gate.main(
            [
                str(raw),
                "--profile",
                "frontend-image",
                "--output-dir",
                str(destination),
            ]
        )
        == 1
    )
    output = (destination / "report.json").read_text(encoding="utf-8")
    assert json.loads(output)["Gate"] == "error"
    assert "private-content" not in output


def test_scanner_error_cannot_be_masked_by_a_valid_report(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(report(image())), encoding="utf-8")
    assert (
        gate.main(
            [
                str(raw),
                "--profile",
                "frontend-image",
                "--scanner-outcome",
                "failure",
                "--output-dir",
                str(tmp_path / "public"),
            ]
        )
        == 1
    )


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "skipped"])
def test_real_security_gate_script_rejects_failed_cancelled_or_skipped_dependencies(outcome):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["security-gate"]
    script = job["steps"][0]["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    results = {name: {"result": "success"} for name in job["needs"]}
    results["security-scan"]["result"] = outcome
    process = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "RESULTS": json.dumps(results)},
        capture_output=True,
        text=True,
    )
    assert process.returncode == int(outcome != "success")


def test_pr_security_gate_includes_every_image_iac_and_lambda_and_blocks_publishing():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert jobs["security-gate"]["if"] == "always()"
    assert set(jobs["security-gate"]["needs"]) >= {
        "security-scan",
        "helm-chart",
        "build-image",
        "build-frontend",
        "cognito-trigger",
    }
    assert "security-gate" in jobs["publish-image"]["needs"]
    action = yaml.safe_load(
        (ROOT / ".github/actions/trivy-scan/action.yml").read_text(encoding="utf-8")
    )
    assert not any(s.get("continue-on-error") for s in action["runs"]["steps"])
    gate_step = next(s for s in action["runs"]["steps"] if "trivy_gate.py" in s.get("run", ""))
    assert "always()" in gate_step["if"]
