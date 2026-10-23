"""Render a Markdown summary of CLIP match scores for the calibration workflow and report.

    python scripts/calibration_summary.py match_scores.csv --report report.json

Needs no ML dependencies: it reads scores already produced by calibrate_match_threshold.py.
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "matching-service"))
sys.path.insert(0, str(ROOT / "scripts"))

from calibrate_match_threshold import read_scores  # noqa: E402
from matching_app.calibration import (  # noqa: E402
    MODALITIES,
    by_modality,
    evaluate,
    threshold_grid,
)
from matching_app.calibration import report as calibration_report  # noqa: E402

BASELINE_THRESHOLD = 0.25


def spread(values):
    if not values:
        return "–"
    return f"{min(values):.3f} / {statistics.median(values):.3f} / {max(values):.3f}"


def fmt(metrics):
    if metrics is None:
        return "none reaches target"
    return (
        f"**{metrics['threshold']:.2f}** (P {metrics['precision']:.2f}, R {metrics['recall']:.2f}, "
        f"TP {metrics['true_positives']}, FP {metrics['false_positives']})"
    )


def summarise(scores, min_precision, report=None):
    result = (
        report if report is not None else calibration_report(scores, min_precision=min_precision)
    )
    min_precision = result["min_precision"]
    groups = by_modality(scores)
    lines = [
        "## Match threshold calibration",
        "",
        f"{len(scores)} labelled pairs, precision target {min_precision}. Scores are CLIP "
        "cosine similarities (pinned ViT-B/32); a pair matches when score >= threshold.",
        "",
        "| Query | Pairs | Positives min/median/max | Negatives min/median/max | "
        f"At historical baseline {BASELINE_THRESHOLD} | Recommended |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    grid = result.get("thresholds", threshold_grid())
    rows = [(m, groups[m]) for m in MODALITIES if m in groups] + [("overall", scores)]
    for name, rows_ in rows:
        current = evaluate(rows_, BASELINE_THRESHOLD)
        best = result["overall"] if name == "overall" else result["modalities"][name]["recommended"]
        lines.append(
            f"| {name} | {len(rows_)} | {spread([r.score for r in rows_ if r.is_match])} | "
            f"{spread([r.score for r in rows_ if not r.is_match])} | "
            f"P {current.precision:.2f} / R {current.recall:.2f} | "
            f"{fmt(best)} |"
        )
    lines += ["", "### Precision / recall by threshold", ""]
    header = "| Threshold | " + " | ".join(name for name, _ in rows) + " |"
    lines += [header, "|" + " --- |" * (len(rows) + 1)]
    for threshold in grid:
        cells = []
        for _, rows_ in rows:
            m = evaluate(rows_, threshold)
            cells.append(f"{m.precision:.2f} / {m.recall:.2f}")
        lines.append(f"| {threshold:.2f} | " + " | ".join(cells) + " |")
    lines += ["", "Cells are precision / recall."]
    if report is not None:
        lines += ["", "<details><summary>report.json</summary>", "", "```json"]
        lines += [json.dumps(report, indent=2), "```", "", "</details>"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scores", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--min-precision", type=float, default=0.8)
    args = parser.parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8")) if args.report else None
    sys.stdout.write(summarise(read_scores(args.scores), args.min_precision, report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
