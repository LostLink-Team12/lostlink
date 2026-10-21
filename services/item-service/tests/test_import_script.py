"""CLI validates local files and exercises real API plus S3 multipart form uploads."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest


def module():
    path = Path(__file__).parents[3] / "scripts/import_items.py"
    spec = importlib.util.spec_from_file_location("lostlink_import_script", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_manifest_cannot_read_photos_outside_its_directory(tmp_path):
    script = module()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([{"client_reference": "one", "photo_path": "../outside.png"}]))
    with pytest.raises(ValueError):
        script.load_manifest(manifest)
    with pytest.raises(ValueError):
        script.ImportClient("http://remote.invalid", "not-a-real-token")


@pytest.mark.integration
def test_import_cli_uses_real_routes_and_presigned_s3_post(item_env, item_s3, tmp_path):
    env, script = item_env, module()
    image = tmp_path / "image.png"
    image.write_bytes(b"png")
    rows = [
        {
            "client_reference": str(index),
            "photo_path": "image.png",
            "title": "Umbrella",
            "description": "A blue umbrella",
            "category": "umbrella",
            "found_at": datetime.now(UTC).isoformat(),
            "verification_questions": [
                {"prompt": "Which initials?", "reference_answer": "Private"}
            ],
        }
        for index in range(2)
    ]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(rows))

    class Client(script.ImportClient):
        def api(self, method, path, body=None, *, key=None, version=None):
            headers = {"Authorization": "Bearer a", "Idempotency-Key": key or uuid4().hex}
            if version:
                headers["If-Match"] = f'"{version}"'
            response = env.client.request(method, path, json=body, headers=headers)
            assert response.status_code < 300, response.text
            return response.json()

        def wait(self, path, complete, timeout):
            while env.worker.once("tasks"):
                pass
            return super().wait(path, complete, timeout)

    client = Client("http://localhost:8003", "not-a-real-token")
    prepared = script.load_manifest(manifest)

    class InterruptedClient(Client):
        def upload(self, *_):
            raise OSError("Simulated interruption after upload-slot creation")

    with pytest.raises(OSError):
        script.run(
            InterruptedClient("http://localhost:8003", "unused"),
            prepared,
            timeout=10,
            emit=lambda _: None,
        )
    assert script.run(client, prepared, timeout=10, emit=lambda _: None) == 0
    assert script.run(client, prepared, timeout=10, emit=lambda _: None) == 0
    assert (
        len(env.client.get("/v1/items", headers={"Authorization": "Bearer a"}).json()["items"]) == 2
    )
