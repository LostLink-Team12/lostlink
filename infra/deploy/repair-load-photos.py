"""Re-upload bounded single-frame derivatives for failed C3 fixture photos.

Original D1 files and packed import identities remain unchanged. Only failed
items in the recorded synthetic acceptance venue can be modified. Original and
derived checksums are retained, and Item's normal versioned API creates a new
upload/correlation; historical failed Matching jobs are never deleted.
Requires the locked Matching Pillow dependency.
"""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

from acceptance import API, login
from configure import LOCAL, Cluster, outputs
from PIL import Image


def main():
    os.environ.update(AWS_PROFILE="lostlink-dev", AWS_DEFAULT_REGION="ap-southeast-1")
    root = Path(__file__).resolve().parents[2]

    class C:
        kubectl = str(root / "infra/terraform/.terraform/tools/kubectl.exe")
        kubeconfig = str(root / "infra/terraform/.terraform/validation/kubeconfig")

    data, cluster = outputs(), Cluster(C())
    users = json.loads(
        base64.b64decode(cluster.get("secret", "lostlink-acceptance")["data"]["users"])
    )
    origin = data["c3"]["value"]["frontend_origin"]
    staff = login(
        users["staff"],
        data["application"]["value"]["operator_client_id"],
        data["cognito"]["value"]["login_domain"],
        origin,
        venue=True,
    )
    api = API(origin)
    venue = api.call("venue", "/v1/venues/current", staff)
    accepted = json.loads((LOCAL / "business-acceptance.json").read_text())
    if venue["id"] != accepted["venues"][0]:
        raise RuntimeError("Refusing a venue other than the synthetic C3 fixture")
    page = api.call("item", "/v1/items?state=processing_failed&limit=100", staff)
    if page["has_more"]:
        raise RuntimeError("Unexpectedly many failed fixture items")
    rows = json.loads((root / "tests/load/generated/manifest.json").read_text(encoding="utf-8"))
    records = []
    output = LOCAL / "photo-repairs.json"
    if output.exists():
        records = json.loads(output.read_text())
    with (root / "tests/load/generated/photos.bin").open("rb") as bundle:
        for item in page["items"]:
            matches = [row for row in rows if row["item"]["title"] == item["title"]]
            if len(matches) != 1 or matches[0]["item"]["description"] != item["description"]:
                raise RuntimeError("Failed item does not match exactly one original D1 fixture")
            row = matches[0]
            bundle.seek(row["offset"])
            original = bundle.read(row["size"])
            if hashlib.sha256(original).hexdigest() != row["sha256"]:
                raise RuntimeError("Original D1 photo checksum mismatch")
            with Image.open(io.BytesIO(original)) as image:
                source_format, source_frames, source_size = (
                    image.format,
                    getattr(image, "n_frames", 1),
                    image.size,
                )
                if (
                    source_frames == 1
                    and source_format == "JPEG"
                    and len(original) <= 5242880
                    and image.width * image.height <= 16000000
                ):
                    raise RuntimeError(
                        "Photo already satisfies input limits; diagnose its actual failure"
                    )
                image.seek(0)
                image = image.convert("RGB")
                image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=90)
                photo, final_size = buffer.getvalue(), image.size
            item = api.call(
                "item",
                f"/v1/items/{item['id']}",
                staff,
                {
                    "description": item["description"].replace(
                        "No modifications.",
                        "Single-frame JPEG derivative resized to at most 2048 pixels for CLIP.",
                    )
                },
                method="PATCH",
                version=item["version"],
            )
            ticket = api.call(
                "item",
                f"/v1/items/{item['id']}/uploads",
                staff,
                {"content_type": "image/jpeg", "size": len(photo)},
                version=item["version"],
            )
            boundary = "lostlink-" + uuid4().hex
            parts = [
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
                    f"\r\n\r\n{value}\r\n"
                ).encode()
                for key, value in ticket["upload"]["fields"].items()
            ]
            parts.extend(
                [
                    (
                        f"--{boundary}\r\nContent-Disposition: form-data; "
                        'name="file"; filename="photo.jpg"\r\n'
                        "Content-Type: image/jpeg\r\n\r\n"
                    ).encode(),
                    photo,
                    f"\r\n--{boundary}--\r\n".encode(),
                ]
            )
            with urlopen(
                Request(
                    ticket["upload"]["url"],
                    data=b"".join(parts),
                    headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
                ),
                timeout=60,
            ) as response:
                if response.status not in {200, 201, 204}:
                    raise RuntimeError("Fixture derivative upload failed")
            api.call(
                "item", f"/v1/items/{item['id']}/uploads/{ticket['upload_id']}/confirm", staff, {}
            )
            api.wait(
                "item",
                f"/v1/items/{item['id']}",
                staff,
                lambda value: value["state"] == "available",
                timeout=300,
            )
            records.append(
                {
                    "item_id": item["id"],
                    "client_reference": row["client_reference"],
                    "original_sha256": row["sha256"],
                    "original_format": source_format,
                    "original_frames": source_frames,
                    "original_size": source_size,
                    "derived_sha256": hashlib.sha256(photo).hexdigest(),
                    "derived_size": final_size,
                    "derived_bytes": len(photo),
                    "final_state": "available",
                }
            )
            output.write_text(json.dumps(records, indent=2), encoding="utf-8")
            print(
                f"Re-uploaded bounded derivative through Item: {item['id']} available", flush=True
            )


if __name__ == "__main__":
    main()
