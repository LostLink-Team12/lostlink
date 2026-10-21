"""Import item manifests in resumable batches, then upload and confirm local photos."""

import argparse
import hashlib
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ImportClient:
    def __init__(self, base_url, access_token):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Use a fixed HTTP(S) Item API URL")
        self.local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not self.local:
            raise ValueError("Remote API endpoints require HTTPS")
        self.base, self.token = base_url.rstrip("/"), access_token
        self.opener = urllib.request.build_opener(NoRedirect)

    def api(self, method, path, body=None, *, key=None, version=None):
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["Idempotency-Key"] = key or uuid4().hex
        if version is not None:
            headers["If-Match"] = f'"{version}"'
        request = urllib.request.Request(
            self.base + path,
            method=method,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"API request failed: {method} {path} HTTP {exc.code}") from None

    def upload(self, presigned, path, content_type):
        target = urlsplit(presigned["url"])
        if (
            target.username
            or target.password
            or target.fragment
            or target.scheme != "https"
            and not (
                self.local
                and target.scheme == "http"
                and target.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ValueError("Upload target must be HTTPS or a local test endpoint")
        boundary = "lostlink-" + uuid4().hex
        chunks = []
        for key, value in presigned["fields"].items():
            if any(character in key for character in ['"', "\r", "\n"]):
                raise ValueError("Invalid upload form field")
            chunks.append(
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
                    f"\r\n\r\n{value}\r\n"
                ).encode()
            )
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="photo"\r\nContent-Type: {content_type}\r\n\r\n'
            ).encode()
        )
        chunks.extend([path.read_bytes(), f"\r\n--{boundary}--\r\n".encode()])
        request = urllib.request.Request(
            presigned["url"],
            method="POST",
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        # Never forward the Cognito token to S3.
        with self.opener.open(request, timeout=30) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError("Photo upload failed")

    def wait(self, path, complete, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.api("GET", path)
            if complete(value):
                return value
            time.sleep(0.5)
        raise TimeoutError(f"Timed out waiting for {path}")


def load_manifest(path):
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("Manifest must be a nonempty JSON array")
    prepared, references = [], set()
    root = path.parent.resolve()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("photo_path"), str):
            raise ValueError("Every row requires a local photo_path")
        reference = row.get("client_reference")
        if not isinstance(reference, str) or reference in references:
            raise ValueError("Manifest requires unique client references")
        references.add(reference)
        image = (root / row["photo_path"]).resolve()
        if not image.is_relative_to(root) or not image.is_file():
            raise ValueError("Photo paths must identify files inside the manifest directory")
        size = image.stat().st_size
        content_type = mimetypes.guess_type(image.name)[0]
        if not 1 <= size <= 10485760 or content_type not in {
            "image/jpeg",
            "image/png",
            "image/webp",
        }:
            raise ValueError("Photos must be JPEG/PNG/WebP and at most 10 MiB")
        prepared.append(
            {
                "entry": {key: value for key, value in row.items() if key != "photo_path"},
                "image": image,
                "content_type": content_type,
                "size": size,
                "photo_hash": hashlib.sha256(image.read_bytes()).hexdigest(),
            }
        )
    return prepared


def run(client, prepared, *, batch_size=100, timeout=600, emit=print):
    if not 1 <= batch_size <= 100:
        raise ValueError("Batch size must be 1-100")
    failures, confirmed = 0, 0
    for start in range(0, len(prepared), batch_size):
        batch = prepared[start : start + batch_size]
        batch_hash = fingerprint(
            [{"entry": row["entry"], "photo_hash": row["photo_hash"]} for row in batch]
        )
        job = client.api(
            "POST",
            "/v1/item-imports",
            {"batch_reference": batch_hash, "entries": [row["entry"] for row in batch]},
            key="batch-" + batch_hash,
        )
        status = client.wait(
            f"/v1/item-imports/{job['id']}?limit=100",
            lambda value: (
                value["job"]["accepted"] + value["job"]["failed"] == value["job"]["total"]
            ),
            timeout,
        )
        by_reference = {row["entry"]["client_reference"]: row for row in batch}
        for entry in status["items"]:
            if entry["state"] != "succeeded":
                failures += 1
                continue
            source = by_reference[entry["client_reference"]]
            item_id = entry["item_id"]
            item = client.api("GET", f"/v1/items/{item_id}")
            if item["state"] in {"processing", "available", "reserved", "handed_over"}:
                confirmed += 1
                continue
            if item["state"] != "awaiting_upload":
                failures += 1
                continue
            upload = client.api(
                "POST",
                f"/v1/items/{item_id}/uploads",
                {"content_type": source["content_type"], "size": source["size"]},
                key="photo-" + fingerprint([item_id, source["photo_hash"], item["version"]]),
                version=item["version"],
            )
            client.upload(upload["upload"], source["image"], source["content_type"])
            operation = client.api(
                "POST",
                f"/v1/items/{item_id}/uploads/{upload['upload_id']}/confirm",
                {},
                key="confirm-" + upload["upload_id"],
            )
            result = client.wait(
                f"/v1/item-operations/{operation['operation_id']}",
                lambda value: value["state"] in {"completed", "failed", "superseded"},
                timeout,
            )
            if result["state"] == "completed":
                confirmed += 1
            else:
                failures += 1
        emit(
            json.dumps(
                {
                    "processed": min(start + batch_size, len(prepared)),
                    "confirmed": confirmed,
                    "failed": failures,
                }
            )
        )
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--token-env", default="LOSTLINK_ACCESS_TOKEN")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        parser.error("Set the access-token environment variable")
    try:
        failures = run(
            ImportClient(args.api_url, token),
            load_manifest(args.manifest),
            batch_size=args.batch_size,
            timeout=args.timeout,
        )
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        # No token, request body, presigned URL or hidden answers are printed.
        parser.exit(1, f"Import stopped: {type(exc).__name__}\n")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
