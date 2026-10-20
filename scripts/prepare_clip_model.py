"""Download the fixed official OpenAI ViT-B/32 checkpoint and verify its SHA256."""

import argparse
import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path

SHA256 = "40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af"
URL = f"https://openaipublic.azureedge.net/clip/models/{SHA256}/ViT-B-32.pt"


def prepare(destination):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        with destination.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() == SHA256:
                return destination
        raise ValueError("Existing checkpoint has the wrong digest; move it before retrying")
    descriptor, temporary = tempfile.mkstemp(prefix="clip-", dir=destination.parent)
    try:
        with (
            os.fdopen(descriptor, "wb") as target,
            urllib.request.urlopen(URL, timeout=30) as response,
        ):
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 400 * 1024 * 1024:
                    raise ValueError("Checkpoint exceeds the download size limit")
                target.write(chunk)
        with open(temporary, "rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != SHA256:
                raise ValueError("Checkpoint digest does not match the official model")
        # Do not overwrite a model installed by a concurrent process.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return prepare(destination)
        return destination
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", default=".models/ViT-B-32.pt")
    args = parser.parse_args()
    print(prepare(args.destination))
    print(f"CLIP_SHA256={SHA256}")


if __name__ == "__main__":
    main()
