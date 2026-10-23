import pytest
from matching_app.calibration import (
    LabelledScore,
    evaluate,
    recommend,
    report,
    threshold_grid,
)


def rows(modality, pairs):
    return [LabelledScore(modality, score, match) for score, match in pairs]


TEXT = rows("text", [(0.31, True), (0.28, True), (0.26, False), (0.22, True), (0.18, False)])
PHOTO = rows("photo", [(0.82, True), (0.74, True), (0.41, False), (0.35, False)])


def test_evaluate_counts_the_worker_rule_score_at_or_above_threshold():
    metrics = evaluate(TEXT, 0.26)
    assert (metrics.true_positives, metrics.false_positives, metrics.false_negatives) == (2, 1, 1)
    assert metrics.precision == pytest.approx(2 / 3)
    assert metrics.recall == pytest.approx(2 / 3)


def test_recommend_picks_lowest_threshold_meeting_precision():
    best = recommend(TEXT, threshold_grid(0.15, 0.35, 0.01), min_precision=0.8)
    assert best.threshold == pytest.approx(0.27)
    assert best.precision == 1.0
    assert best.recall == pytest.approx(2 / 3)


def test_recommend_returns_none_without_positives_or_reachable_precision():
    negatives = rows("text", [(0.5, False), (0.4, False)])
    assert recommend(negatives, threshold_grid(), 0.8) is None
    mixed = rows("text", [(0.3, False), (0.3, True)])
    assert recommend(mixed, [0.3], 0.9) is None


def test_report_separates_modalities():
    result = report(TEXT + PHOTO, threshold_grid(0.10, 0.90, 0.01), min_precision=0.8)
    assert set(result["modalities"]) == {"text", "photo"}
    assert result["modalities"]["photo"]["recommended"]["threshold"] == pytest.approx(0.42)
    assert result["modalities"]["text"]["recommended"]["threshold"] == pytest.approx(0.27)
    assert result["modalities"]["text"]["positives"] == 3


@pytest.mark.parametrize("modality, score", [("audio", 0.2), ("text", 1.5), ("text", float("nan"))])
def test_labelled_score_rejects_invalid_values(modality, score):
    with pytest.raises(ValueError):
        LabelledScore(modality, score, True)


def test_threshold_grid_is_inclusive_and_validated():
    assert threshold_grid(0.2, 0.25, 0.01) == [0.2, 0.21, 0.22, 0.23, 0.24, 0.25]
    with pytest.raises(ValueError):
        threshold_grid(0.3, 0.2, 0.01)


@pytest.mark.parametrize("custom_range", [False, True])
def test_cli_and_markdown_share_high_score_recommendations(
    tmp_path, monkeypatch, capsys, custom_range
):
    import json
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from calibrate_match_threshold import main as calibrate
    from calibration_summary import main as summarise

    scores = tmp_path / "scores.csv"
    scores.write_text(
        "modality,score,is_match\ntext+photo,0.81,1\ntext+photo,0.70,0\n",
        encoding="utf-8",
    )
    options = ["--start", "0.10", "--stop", "0.45"] if custom_range else []
    assert calibrate(["--scores", str(scores), "--min-precision", "0.9", *options]) == 0
    result = json.loads(capsys.readouterr().out)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(result), encoding="utf-8")
    assert summarise([str(scores), "--report", str(report_path)]) == 0
    markdown = capsys.readouterr().out
    assert "precision target 0.9" in markdown
    if custom_range:
        assert result["overall"] is None
        assert "none reaches target" in markdown
        assert "**0.71**" not in markdown
        assert "| 0.71 |" not in markdown
    else:
        assert result["overall"]["threshold"] == pytest.approx(0.71)
        assert result["modalities"]["text+photo"]["recommended"]["threshold"] == pytest.approx(0.71)
        assert "**0.71** (P 1.00, R 1.00, TP 1, FP 0)" in markdown
        assert "| 0.71 |" in markdown


def test_summary_without_json_uses_the_same_default_grid(monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from calibration_summary import summarise

    scores = rows("photo", [(0.82, True), (0.73, False)])
    assert "**0.74**" in summarise(scores, 0.8)
    assert report(scores)["overall"]["threshold"] == pytest.approx(0.74)
