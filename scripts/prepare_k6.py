"""Download a pinned official k6 binary into build/ and verify its release SHA256."""

import argparse
import hashlib
import io
import os
import platform
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.4.0"
# Digests published on grafana/k6's v1.4.0 GitHub release.
ARCHIVES = {
    "Windows": (
        "windows-amd64.zip",
        "aaf1c23e0ed3ef763231e10685943865992e32af84d8894dd9a748f408c0e1e2",
    ),
    "Linux": (
        "linux-amd64.tar.gz",
        "58fa87283f5d5b11bc6824d9204e8a38266c9977f2c4d0a960ce1b5beeaae517",
    ),
}


def prepare(destination=None):
    system = platform.system()
    if system not in ARCHIVES or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Use an independently installed k6 on this platform")
    filename, digest = ARCHIVES[system]
    executable = "k6.exe" if system == "Windows" else "k6"
    target = Path(destination or ROOT / "build/tools/k6" / executable).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    archive_path = target.parent / f"k6-v{VERSION}-{filename}"
    if not archive_path.is_file():
        url = f"https://github.com/grafana/k6/releases/download/v{VERSION}/{archive_path.name}"
        with urllib.request.urlopen(url, timeout=60) as response:
            content = response.read()
    else:
        content = archive_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise RuntimeError("k6 archive checksum mismatch; no executable was installed")
    archive_path.write_bytes(content)
    member = f"k6-v{VERSION}-{filename.split('.')[0]}/{executable}"
    if system == "Windows":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            binary = archive.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            binary = archive.extractfile(member).read()
    temporary = target.with_suffix(".download")
    temporary.write_bytes(binary)
    temporary.chmod(0o755)
    os.replace(temporary, target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path)
    print(prepare(parser.parse_args().destination))
