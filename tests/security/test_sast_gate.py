import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sast_gate", ROOT / "scripts/sast_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def sarif(score="8.0", *, results=True):
    return {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "CodeQL",
                        "rules": [
                            {
                                "id": "test/security",
                                "properties": {"security-severity": score, "tags": ["security"]},
                            }
                        ],
                    }
                },
                "results": [{"ruleId": "test/security", "suppressions": [{"kind": "external"}]}]
                if results
                else [],
            }
        ],
    }


@pytest.mark.parametrize(("score", "blocking"), [("9.8", 1), ("7.0", 1), ("6.9", 0), ("0.0", 0)])
def test_threshold_uses_security_score_and_cannot_be_dismissed(score, blocking):
    assert gate.evaluate(sarif(score))["blocking"] == blocking


def test_completed_clean_analysis_passes_with_query_inventory():
    assert gate.evaluate(sarif(results=False))["blocking"] == 0


@pytest.mark.parametrize("score", ["nan", "inf", "-1", "11", "invalid"])
def test_invalid_scores_cannot_pass(score):
    with pytest.raises(ValueError):
        gate.evaluate(sarif(score))


def test_missing_severity_unknown_rule_and_failed_execution_are_errors():
    for field in ("severity", "rule", "execution"):
        data = sarif()
        run = data["runs"][0]
        if field == "severity":
            del run["tool"]["driver"]["rules"][0]["properties"]["security-severity"]
        elif field == "rule":
            run["results"][0]["ruleId"] = "missing"
        else:
            run["invocations"] = [{"executionSuccessful": False}]
        with pytest.raises((ValueError, KeyError)):
            gate.evaluate(data)


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped"])
def test_report_cannot_hide_unsuccessful_analyzer(tmp_path, outcome):
    (tmp_path / "python.sarif").write_text(json.dumps(sarif(results=False)), encoding="utf-8")
    assert (
        gate.main(
            [
                str(tmp_path),
                "--language",
                "python",
                "--analysis-outcome",
                outcome,
                "--output-dir",
                str(tmp_path / "public"),
            ]
        )
        == 1
    )


def test_absent_report_produces_a_failure_artifact(tmp_path):
    assert (
        gate.main(
            [
                str(tmp_path),
                "--language",
                "python",
                "--output-dir",
                str(tmp_path / "public"),
            ]
        )
        == 1
    )
    assert json.loads((tmp_path / "public/report.json").read_text())["gate"] == "error"


def test_sast_is_required_by_builds_and_final_pr_gate():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    for name in ("build-image", "build-frontend", "security-gate"):
        assert "sast" in jobs[name]["needs"]
    analysis = yaml.safe_load((ROOT / ".github/workflows/sast.yml").read_text())["jobs"]["analyze"]
    assert analysis["strategy"]["matrix"]["language"] == ["python", "javascript-typescript"]
