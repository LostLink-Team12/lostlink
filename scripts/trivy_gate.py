"""Fail closed on incomplete Trivy scans; publish only allowlisted report fields."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = (
    "repository",
    "python-image",
    "matching-image",
    "frontend-image",
    "lambda",
    "kubernetes",
)
LOCKS = (
    "requirements.lock",
    "requirements-dev.lock",
    "services/matching-service/requirements-ml.lock",
    "frontend/package-lock.json",
    "tests/data/package-lock.json",
)
RENDERED_MANIFESTS = {
    "cd-rbac.yaml",
    "venue.yaml",
    "item.yaml",
    "matching.yaml",
    "claim.yaml",
    "notification.yaml",
    "platform.yaml",
    "umbrella.yaml",
    "umbrella-migrations.yaml",
}
SEVERITIES = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
HIGH = {"HIGH", "CRITICAL"}
PIN = re.compile(r"^([\w.-]+)(?:\[[^]]+\])?==([^\s;]+)")


class CoverageError(ValueError):
    """An actionable error built only from inventory names, never source contents."""


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def expected_packages(path):
    if path.name == "package-lock.json":
        packages = json.loads(path.read_text(encoding="utf-8"))["packages"]
        return {
            (canonical(info.get("name", name.rsplit("node_modules/", 1)[-1])), info["version"])
            for name, info in packages.items()
            if name and not info.get("link")
        }
    return {
        (canonical(match[1]), match[2])
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := PIN.match(line))
    }


def entries(result, key):
    value = result.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"Invalid {key} list")
    return value


def packages(results):
    return {
        (canonical(item["Name"]), item["Version"])
        for result in results
        for item in entries(result, "Packages")
    }


def target_matches(target, path):
    target = target.replace("\\", "/").split(" (", 1)[0]
    path = str(path).replace("\\", "/")
    return target == path or target.endswith("/" + path)


def check_coverage(results, profile, root, scan_ref):
    coverage = []
    if profile == "repository":
        for name in LOCKS:
            matches = [r for r in results if target_matches(r["Target"], name)]
            expected = expected_packages(root / name)
            actual = packages(matches)
            if not expected or not expected <= actual:
                raise CoverageError(f"Incomplete package inventory: {name}")
            coverage.append({"target": name, "packages": len(actual)})
        for kind in ("terraform", "dockerfile"):
            if not any(r.get("Type") == kind and r.get("MisconfSummary") for r in results):
                raise CoverageError(f"Missing IaC inventory: {kind}")
    elif profile == "kubernetes":
        manifests = sorted(Path(scan_ref).glob("*.yaml"))
        missing = RENDERED_MANIFESTS - {path.name for path in manifests}
        if missing:
            raise CoverageError("Missing rendered manifests: " + ", ".join(sorted(missing)))
        for manifest in manifests:
            if not any(
                r.get("Type") == "kubernetes"
                and r.get("MisconfSummary")
                and target_matches(r["Target"], manifest.name)
                for r in results
            ):
                raise CoverageError(f"Missing rendered Kubernetes inventory: {manifest.name}")
            coverage.append({"target": manifest.name, "scanned": True})
    else:
        installed = packages(results)
        if profile != "lambda" and not any(
            r.get("Class") == "os-pkgs" and entries(r, "Packages") for r in results
        ):
            raise CoverageError("Missing image OS package inventory")
        if profile != "frontend-image":
            required = {"fastapi", "starlette"}
            if profile == "matching-image":
                required.add("torch")
            if not required <= {name for name, _ in installed}:
                raise CoverageError("Missing Python runtime package inventory")
        coverage.append({"target": profile, "packages": len(installed)})
    return coverage


def allow_fields(item, names):
    return {name: item[name] for name in names if name in item}


def evaluate(report, profile, *, root=ROOT, scan_ref="."):
    if not isinstance(report, dict) or report.get("SchemaVersion") != 2:
        raise ValueError("Unsupported or missing Trivy report schema")
    results = report.get("Results")
    if not isinstance(results, list) or not results:
        raise ValueError("Missing Trivy scan results")
    redacted = []
    blocked = 0
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("Target"), str):
            raise ValueError("Invalid Trivy result")
        safe = allow_fields(result, ("Target", "Class", "Type"))
        if result.get("MisconfSummary"):
            safe["MisconfSummary"] = allow_fields(
                result["MisconfSummary"], ("Successes", "Failures", "Exceptions")
            )
        safe["Packages"] = [
            allow_fields(p, ("Name", "Version")) for p in entries(result, "Packages")
        ]
        for key, fields in {
            "Vulnerabilities": (
                "VulnerabilityID",
                "PkgName",
                "InstalledVersion",
                "FixedVersion",
                "Status",
                "Severity",
                "Title",
                "PrimaryURL",
            ),
            "Misconfigurations": ("ID", "AVDID", "Title", "Severity", "Status", "PrimaryURL"),
            # Never publish Match, Code, raw source lines or arbitrary metadata.
            "Secrets": ("RuleID", "Category", "Title", "Severity", "StartLine", "EndLine"),
        }.items():
            safe[key] = []
            for finding in entries(result, key):
                severity = finding.get("Severity")
                if severity not in SEVERITIES:
                    raise ValueError("Missing or invalid finding severity")
                if key == "Vulnerabilities":
                    fail = severity in HIGH and bool(finding.get("FixedVersion"))
                elif key == "Misconfigurations":
                    if finding.get("Status") not in {"PASS", "FAIL", "EXCEPTION"}:
                        raise ValueError("Missing or invalid misconfiguration status")
                    fail = severity in HIGH and finding["Status"] == "FAIL"
                else:
                    fail = True  # A secret has no vendor fix to wait for.
                blocked += fail
                safe[key].append({**allow_fields(finding, fields), "Blocking": fail})
        redacted.append(safe)
    coverage = check_coverage(results, profile, root, scan_ref)
    return {"Results": redacted, "Coverage": coverage, "BlockingFindings": blocked}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--profile", required=True, choices=PROFILES)
    parser.add_argument("--scan-ref", default=".")
    parser.add_argument("--scanner-outcome", default="success")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    output = {
        "SchemaVersion": 1,
        "Profile": args.profile,
        "SourceSHA": os.environ.get("GITHUB_SHA"),
        "ScannerOutcome": args.scanner_outcome,
        "Gate": "error",
    }
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        output.update(evaluate(report, args.profile, scan_ref=args.scan_ref))
        output["Gate"] = "failed" if output["BlockingFindings"] else "passed"
        if args.scanner_outcome != "success":
            output.update(Gate="error", Error="Trivy scanner did not complete successfully")
    except CoverageError as exc:
        output["Error"] = str(exc)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Do not include exception text: malformed input could contain secret data.
        output["Error"] = f"Invalid or incomplete scan evidence ({type(exc).__name__})"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(
        json.dumps(output, indent=2) + "\n", encoding="utf-8"
    )
    summary = (
        f"Trivy {args.profile}: {output['Gate']}; "
        f"blocking findings: {output.get('BlockingFindings', 'unknown')}\n"
    )
    (args.output_dir / "summary.md").write_text(summary, encoding="utf-8")
    print(summary, end="")
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_path, "a", encoding="utf-8") as stream:
            stream.write(summary + "\n")
    return 0 if output["Gate"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
