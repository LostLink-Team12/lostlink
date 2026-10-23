"""Calibrate MATCH_THRESHOLD for matching-service from labelled demo pairs.

Two inputs are supported, both CSV with a header row:

1. --pairs: labelled query/item pairs scored with the real pinned CLIP model, exactly as
   the worker embeds items (description + photo) and the API embeds queries.
       query_text,query_photo,item_description,item_photo,is_match
   Photo columns are file paths relative to the CSV (empty when absent). is_match is 1 or 0.
   Requires the ML dependencies (services/matching-service/requirements-ml.lock) and
   CLIP_CHECKPOINT / CLIP_SHA256 (see scripts/prepare_clip_model.py).

2. --scores: scores that were already computed, e.g. by an earlier --pairs run.
       modality,score,is_match

Usage from the repository root:

    python scripts/calibrate_match_threshold.py --pairs tests/data/match_pairs.csv \
        --write-scores tests/data/match_scores.csv
    python scripts/calibrate_match_threshold.py --scores tests/data/match_scores.csv \
        --min-precision 0.8

The report gives a recommended threshold per query modality and overall. Today the
worker selects MATCH_THRESHOLD for photo reports and MATCH_THRESHOLD_TEXT for text-only
reports. Calibration never changes runtime configuration automatically.
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "matching-service"))

from matching_app.calibration import (  # noqa: E402
    DEFAULT_START,
    DEFAULT_STEP,
    DEFAULT_STOP,
    LabelledScore,
    report,
    threshold_grid,
)


def truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y"}


def read_scores(path: Path) -> list[LabelledScore]:
    with path.open(newline="", encoding="utf-8") as source:
        return [
            LabelledScore(row["modality"].strip(), float(row["score"]), truthy(row["is_match"]))
            for row in csv.DictReader(source)
        ]


def score_pairs(path: Path, checkpoint: str, sha256: str, device: str) -> list[LabelledScore]:
    from matching_app.embedding import ClipEncoder

    encoder = ClipEncoder(checkpoint, sha256, device)
    encoder.ready()
    base = path.parent
    item_cache: dict[tuple[str, str], list[float]] = {}

    def photo(value: str) -> bytes | None:
        return (base / value).read_bytes() if value.strip() else None

    results = []
    with path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            text = row["query_text"].strip() or None
            query_photo = photo(row["query_photo"])
            if text and query_photo:
                modality = "text+photo"
            elif query_photo:
                modality = "photo"
            elif text:
                modality = "text"
            else:
                raise ValueError("Each pair needs query_text, query_photo or both")
            key = (row["item_description"].strip(), row["item_photo"].strip())
            if key not in item_cache:
                item_cache[key] = encoder.encode(key[0], photo(key[1]))
            query = encoder.encode(text, query_photo)
            # Both vectors are unit length, so the dot product is the cosine similarity.
            score = sum(a * b for a, b in zip(query, item_cache[key], strict=True))
            results.append(
                LabelledScore(modality, max(-1.0, min(1.0, score)), truthy(row["is_match"]))
            )
    return results


def write_scores(path: Path, scores: list[LabelledScore]) -> None:
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.writer(target)
        writer.writerow(["modality", "score", "is_match"])
        for row in scores:
            writer.writerow([row.modality, f"{row.score:.6f}", int(row.is_match)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pairs", type=Path, help="labelled query/item pairs to score")
    source.add_argument("--scores", type=Path, help="precomputed modality,score,is_match rows")
    parser.add_argument("--write-scores", type=Path, help="save computed scores as CSV")
    parser.add_argument("--min-precision", type=float, default=0.8)
    parser.add_argument("--start", type=float, default=DEFAULT_START)
    parser.add_argument("--stop", type=float, default=DEFAULT_STOP)
    parser.add_argument("--step", type=float, default=DEFAULT_STEP)
    parser.add_argument("--checkpoint", default=os.getenv("CLIP_CHECKPOINT"))
    parser.add_argument("--sha256", default=os.getenv("CLIP_SHA256"))
    parser.add_argument("--device", default=os.getenv("CLIP_DEVICE", "cpu"))
    args = parser.parse_args(argv)

    if args.pairs:
        if not (args.checkpoint and args.sha256):
            parser.error("--pairs needs CLIP_CHECKPOINT and CLIP_SHA256")
        scores = score_pairs(args.pairs, args.checkpoint, args.sha256, args.device)
        if args.write_scores:
            write_scores(args.write_scores, scores)
    else:
        scores = read_scores(args.scores)

    grid = threshold_grid(args.start, args.stop, args.step)
    print(json.dumps(report(scores, grid, args.min_precision), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
