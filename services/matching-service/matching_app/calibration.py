"""Offline calibration of the automatic match threshold (MATCH_THRESHOLD).

CLIP cosine scores depend on the query modality: a text-only query compared with a
fused text+photo item vector scores lower than a photo query of the same object.
This module turns labelled (score, is_match) pairs into precision/recall per
threshold and recommends the lowest threshold that still meets a precision target,
separately for each modality. It has no ML or database dependency so it can be unit
tested; scripts/calibrate_match_threshold.py produces the scores with the real model.
"""

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

MODALITIES = ("text", "photo", "text+photo")


@dataclass(frozen=True)
class LabelledScore:
    modality: str
    score: float
    is_match: bool

    def __post_init__(self):
        if self.modality not in MODALITIES:
            raise ValueError(f"Unknown modality {self.modality!r}")
        if not math.isfinite(self.score) or not -1.0 <= self.score <= 1.0:
            raise ValueError("Scores are cosine similarities in [-1, 1]")


@dataclass(frozen=True)
class ThresholdMetrics:
    threshold: float
    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float:
        predicted = self.true_positives + self.false_positives
        return self.true_positives / predicted if predicted else 1.0

    @property
    def recall(self) -> float:
        actual = self.true_positives + self.false_negatives
        return self.true_positives / actual if actual else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def evaluate(scores: Iterable[LabelledScore], threshold: float) -> ThresholdMetrics:
    """Matching rule used by the worker: a pair matches when score >= threshold."""
    tp = fp = fn = 0
    for row in scores:
        predicted = row.score >= threshold
        if predicted and row.is_match:
            tp += 1
        elif predicted:
            fp += 1
        elif row.is_match:
            fn += 1
    return ThresholdMetrics(threshold, tp, fp, fn)


def threshold_grid(start: float = 0.10, stop: float = 0.45, step: float = 0.01) -> list[float]:
    if step <= 0 or stop < start:
        raise ValueError("Use a positive step and stop >= start")
    count = round((stop - start) / step)
    return [round(start + i * step, 4) for i in range(count + 1)]


def sweep(scores: Sequence[LabelledScore], thresholds: Sequence[float]) -> list[ThresholdMetrics]:
    return [evaluate(scores, t) for t in thresholds]


def recommend(
    scores: Sequence[LabelledScore], thresholds: Sequence[float], min_precision: float = 0.8
) -> ThresholdMetrics | None:
    """Lowest threshold (best recall) whose precision meets the target.

    Missing a real match costs an owner their item, while a false alert costs one
    email, so we keep recall as high as the precision target allows. Returns None
    when no threshold reaches the target or there are no positive examples.
    """
    if not any(row.is_match for row in scores):
        return None
    for metrics in sweep(scores, sorted(thresholds)):
        if metrics.true_positives and metrics.precision >= min_precision:
            return metrics
    return None


def by_modality(scores: Iterable[LabelledScore]) -> dict[str, list[LabelledScore]]:
    groups: dict[str, list[LabelledScore]] = defaultdict(list)
    for row in scores:
        groups[row.modality].append(row)
    return dict(groups)


def report(
    scores: Sequence[LabelledScore],
    thresholds: Sequence[float] | None = None,
    min_precision: float = 0.8,
) -> dict:
    """Recommendation per modality plus the single threshold the worker uses today."""
    grid = list(thresholds or threshold_grid())
    result = {"min_precision": min_precision, "modalities": {}}
    for modality, rows in sorted(by_modality(scores).items()):
        best = recommend(rows, grid, min_precision)
        result["modalities"][modality] = {
            "pairs": len(rows),
            "positives": sum(row.is_match for row in rows),
            "recommended": _as_dict(best) if best else None,
        }
    overall = recommend(scores, grid, min_precision)
    result["overall"] = _as_dict(overall) if overall else None
    return result


def _as_dict(metrics: ThresholdMetrics) -> dict:
    return {
        "threshold": metrics.threshold,
        "precision": round(metrics.precision, 4),
        "recall": round(metrics.recall, 4),
        "f1": round(metrics.f1, 4),
        "true_positives": metrics.true_positives,
        "false_positives": metrics.false_positives,
        "false_negatives": metrics.false_negatives,
    }
