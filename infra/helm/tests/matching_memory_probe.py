"""Run inside the ML image, under the chart's Docker memory/swap limits."""

import io
import json
import math
import os
import resource
from pathlib import Path

from matching_app.config import MatchingSettings
from matching_app.embedding import ClipEncoder
from PIL import Image


def main():
    mode = os.environ["PROBE_MODE"]
    settings = MatchingSettings(
        _env_file=None, process_mode="worker" if mode == "tasks" else "api", worker_mode="tasks"
    )
    if mode == "api":
        from matching_app.main import create_app

        app = create_app(settings)
        encoder = app.state.encoder
    elif mode == "tasks":
        from matching_app.workers import MatchingWorker

        worker = MatchingWorker(
            None,
            encoder=ClipEncoder(settings.clip_checkpoint, settings.clip_sha256, "cpu"),
            settings=settings,
        )
        encoder = worker.encoder
    else:
        raise ValueError("Unknown process")
    encoder.ready()
    photo = io.BytesIO()
    # Exercise the permitted 16-million-pixel boundary, not just a thumbnail.
    image = Image.new("RGB", (4000, 4000), "red")
    image.save(photo, format="JPEG")
    image.close()
    for text, content in [
        ("red backpack", None),
        (None, photo.getvalue()),
        ("red backpack", photo.getvalue()),
    ]:
        vector = encoder.encode(text, content)
        if len(vector) != 512 or not all(math.isfinite(value) for value in vector):
            raise RuntimeError("Expected 512 finite values")
        if not math.isclose(sum(value * value for value in vector), 1.0, abs_tol=1e-5):
            raise RuntimeError("Expected a unit vector")
    peak_file = Path("/sys/fs/cgroup/memory.peak")
    print(
        json.dumps(
            {
                "process": mode,
                "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
                "cgroup_peak_bytes": peak_file.read_text().strip() if peak_file.exists() else None,
                "inference": "text/photo/fused",
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
