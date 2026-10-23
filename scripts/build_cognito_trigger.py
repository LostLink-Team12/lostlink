"""Build a Python 3.12/x86_64 Cognito trigger ZIP using the AWS Linux runtime.

Run from any directory: python scripts/build_cognito_trigger.py
Requires Docker Buildx; no AWS credentials or cloud resources are used.
"""

import argparse
import hashlib
import json
import ssl
import stat
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
RDS_CA_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"


def build(output: Path, rds_ca: Path | None = None) -> None:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cognito-build-", dir=output.parent) as temporary:
        staging = Path(temporary) / "package"
        subprocess.run(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/amd64",
                "--file",
                str(ROOT / "services/venue-service/Dockerfile.lambda"),
                "--target",
                "artifact",
                "--output",
                f"type=local,dest={staging}",
                str(ROOT),
            ],
            check=True,
        )
        certificate = staging / "rds-global-bundle.pem"
        if rds_ca is None:
            with urllib.request.urlopen(RDS_CA_URL, timeout=30) as response:
                certificate.write_bytes(response.read())
        else:
            certificate.write_bytes(rds_ca.read_bytes())
        # Reject malformed PEM input before publishing the generated artifact.
        ssl.create_default_context(cafile=str(certificate))
        files = sorted(
            path
            for path in staging.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        )
        total = sum(path.stat().st_size for path in files)
        if total > MAX_UNCOMPRESSED_BYTES:
            raise RuntimeError("Function exceeds Lambda's 250 MiB uncompressed ZIP limit")
        archive = Path(temporary) / "function.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in files:
                info = zipfile.ZipInfo(path.relative_to(staging).as_posix())
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                bundle.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
        with archive.open("rb") as content:
            digest = hashlib.file_digest(content, "sha256").hexdigest()
        manifest = {
            "runtime": "python3.12",
            "architecture": "x86_64",
            "handler": "venue_app.token_trigger.handler",
            "build_image": "public.ecr.aws/lambda/python:3.12",
            "rds_ca_sha256": hashlib.sha256(certificate.read_bytes()).hexdigest(),
            "requirements_sha256": hashlib.sha256(
                (ROOT / "requirements.lock").read_bytes()
            ).hexdigest(),
            "zip_sha256": digest,
            "uncompressed_bytes": total,
            "files": len(files),
        }
        archive.replace(output)
        output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="utf-8")
        output.with_suffix(".manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    print(f"Built {output} ({total / 1024 / 1024:.1f} MiB uncompressed)")
    print(f"SHA256: {digest}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build/cognito-token-trigger.zip",
        help="Output ZIP path",
    )
    parser.add_argument(
        "--rds-ca",
        type=Path,
        help="Reviewed RDS PEM bundle; defaults to AWS's public global bundle",
    )
    args = parser.parse_args()
    build(args.output, args.rds_ca)


if __name__ == "__main__":
    main()
