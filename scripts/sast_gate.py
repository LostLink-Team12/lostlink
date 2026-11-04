"""Evaluate CodeQL SARIF independently of GitHub alert processing or dismissals."""

import argparse
import json
import math
import os
from pathlib import Path


def evaluate(sarif):
    if not isinstance(sarif, dict) or sarif.get("version") != "2.1.0":
        raise ValueError("Expected SARIF 2.1.0")
    runs = sarif.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("Missing analysis runs")
    findings, rule_count = [], 0
    for run in runs:
        driver = run["tool"]["driver"]
        if "CodeQL" not in driver["name"]:
            raise ValueError("Unexpected analysis tool")
        rules = {}
        for component in [driver, *run["tool"].get("extensions", [])]:
            for rule in component.get("rules", []):
                rules[rule["id"]] = rule
        if not rules or not isinstance(run.get("results"), list):
            raise ValueError("Missing query inventory or results")
        rule_count += len(rules)
        for invocation in run.get("invocations", []):
            if invocation.get("executionSuccessful") is False:
                raise ValueError("Analysis invocation failed")
            if any(
                n.get("level") == "error" for n in invocation.get("toolExecutionNotifications", [])
            ):
                raise ValueError("Analysis reported an execution error")
        for result in run["results"]:
            rule = rules[result["ruleId"]]
            properties = rule.get("properties", {})
            score = properties.get("security-severity")
            if score is not None:
                score = float(score)
                if not math.isfinite(score) or not 0 <= score <= 10:
                    raise ValueError("Invalid security severity")
            elif "security" in properties.get("tags", []):
                raise ValueError("Security finding has no severity")
            level = result.get(
                "level", rule.get("defaultConfiguration", {}).get("level", "warning")
            )
            blocked = score >= 7 if score is not None else level == "error"
            locations = []
            for location in result.get("locations", []):
                physical = location.get("physicalLocation", {})
                locations.append(
                    {
                        "file": physical.get("artifactLocation", {}).get("uri"),
                        "line": physical.get("region", {}).get("startLine"),
                    }
                )
            # No snippets, source contents, trace messages or suppression bypasses.
            findings.append(
                {
                    "rule": result["ruleId"],
                    "score": score,
                    "blocking": blocked,
                    "locations": locations,
                }
            )
    return {
        "rules": rule_count,
        "findings": findings,
        "blocking": sum(f["blocking"] for f in findings),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--language", choices=["python", "javascript-typescript"], required=True)
    parser.add_argument("--analysis-outcome", default="success")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    output = {"source_sha": os.getenv("GITHUB_SHA"), "language": args.language, "gate": "error"}
    try:
        files = list(args.directory.glob("*.sarif"))
        if len(files) != 1:
            raise ValueError("Expected exactly one language report")
        output.update(evaluate(json.loads(files[0].read_text(encoding="utf-8"))))
        if args.analysis_outcome != "success":
            raise ValueError("Analysis did not succeed")
        output["gate"] = "failed" if output["blocking"] else "passed"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        output["error"] = f"Missing, invalid or unsuccessful CodeQL evidence ({type(exc).__name__})"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(
        json.dumps(output, indent=2) + "\n", encoding="utf-8"
    )
    summary = (
        f"CodeQL {args.language}: {output['gate']}; blocking: {output.get('blocking', 'unknown')}\n"
    )
    print(summary, end="")
    if path := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(summary + "\n")
    return int(output["gate"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
