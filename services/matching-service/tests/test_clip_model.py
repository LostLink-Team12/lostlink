"""Optional real model acceptance; no random or hash-based embedding substitute."""

import io
import json
import os
import statistics
import time

import pytest
from matching_app.embedding import ClipEncoder, EmbeddingError

pytestmark = pytest.mark.model


def test_real_clip_text_and_photo_vectors_share_space():
    checkpoint, sha = os.getenv("CLIP_CHECKPOINT"), os.getenv("CLIP_SHA256")
    if not checkpoint or not sha:
        pytest.skip("Set CLIP_CHECKPOINT/CLIP_SHA256 and install requirements-ml.lock")
    Image = pytest.importorskip("PIL.Image")
    encoder = ClipEncoder(checkpoint, sha)

    def photo(color):
        image = Image.new("RGB", (224, 224), color)
        content = io.BytesIO()
        image.save(content, format="PNG")
        return content.getvalue()

    text = encoder.encode("a solid red square")
    red = encoder.encode(photo=photo("red"))
    blue = encoder.encode(photo=photo("blue"))
    assert len(text) == len(red) == len(blue) == 512
    assert sum(a * b for a, b in zip(text, red, strict=True)) > sum(
        a * b for a, b in zip(text, blue, strict=True)
    )
    assert sum(x * x for x in red) == pytest.approx(1.0)
    with pytest.raises(EmbeddingError) as error:
        encoder.encode(photo=b"invalid")
    assert error.value.code == "invalid_image" and not error.value.retryable


@pytest.mark.integration
def test_real_clip_item_worker_pgvector_and_photo_search(matching_env):
    from matching_support import headers, index_item, new_report

    checkpoint, sha = os.getenv("CLIP_CHECKPOINT"), os.getenv("CLIP_SHA256")
    if not checkpoint or not sha:
        pytest.skip("Set CLIP_CHECKPOINT/CLIP_SHA256")
    Image = pytest.importorskip("PIL.Image")
    env = matching_env
    encoder = ClipEncoder(checkpoint, sha)
    env.app.state.encoder = env.worker.encoder = encoder
    image = Image.new("RGB", (224, 224), "red")
    content = io.BytesIO()
    image.save(content, format="PNG")
    env.storage.content = content.getvalue()
    item_id = index_item(env, description="a solid red square")
    import base64

    result = env.client.post(
        "/v1/search",
        headers=headers(),
        json={
            "photo_base64": base64.b64encode(content.getvalue()).decode(),
            "min_score": -1,
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["items"][0]["item_id"] == str(item_id)
    assert result.json()["model_version"].startswith("clip-vit-b32-")
    # Small local diagnostic only: real model/DB, injected auth and in-process Item HTTP.
    # This is not a cloud/network p95 or throughput acceptance measurement.
    timings = []
    for _ in range(5):
        started = time.perf_counter()
        response = env.client.post(
            "/v1/search",
            headers=headers(),
            json={"text": "a solid red square", "limit": 1},
        )
        timings.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200, response.text
        assert response.json()["items"][0]["item_id"] == str(item_id)
    metrics = env.app.state.search_metrics
    phases = {
        sample.labels["phase"]: round(sample.value * 1000 / 6, 3)
        for family in metrics.phase_seconds.collect()
        for sample in family.samples
        if sample.name == "lostlink_search_phase_duration_seconds_sum"
    }
    print(
        "LOCAL_SEARCH_DIAGNOSTICS "
        + json.dumps(
            {
                "scope": "local CPU real CLIP + PostgreSQL + ASGI; one synthetic item",
                "warm_text_requests": len(timings),
                "median_ms": round(statistics.median(timings), 3),
                "max_ms": round(max(timings), 3),
                "phase_mean_ms_photo_and_text_requests": phases,
            }
        )
    )
    report = new_report(env, description="a solid red square")
    from matching_support import drain

    drain(env)
    matches = env.client.get(f"/v1/lost-reports/{report['id']}/matches", headers=headers())
    assert matches.status_code == 200 and matches.json()["items"], matches.text
