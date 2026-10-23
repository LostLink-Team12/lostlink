"""Download the pinned CLIP checkpoint into the pod's model volume and verify SHA256.

Mirrors scripts/prepare_clip_model.py; the inference containers mount the volume read-only
and verify the digest again before loading (CLIP_SHA256).
"""

import hashlib
import os
import urllib.request
from pathlib import Path

url, digest = os.environ["MODEL_URL"], os.environ["MODEL_SHA256"]
target = Path(os.environ["MODEL_PATH"])
limit = 400 * 1024 * 1024


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


if target.is_file() and sha256(target) == digest:
    print("model already present")
    raise SystemExit(0)
target.parent.mkdir(parents=True, exist_ok=True)
partial = target.with_suffix(".partial")
size = 0
with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as out:
    while chunk := response.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise SystemExit("checkpoint exceeds the size limit")
        out.write(chunk)
if sha256(partial) != digest:
    partial.unlink()
    raise SystemExit("checkpoint digest does not match CLIP_SHA256")
partial.rename(target)
print(f"model verified: {size} bytes")
