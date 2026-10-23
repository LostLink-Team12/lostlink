"""Write the system CA bundle plus the LostLink internal CA for SSL_CERT_FILE.

Service-to-service calls use fixed HTTPS URLs; Cognito and AWS endpoints still need the
public roots, so the internal CA is appended to (not substituted for) the system bundle.
"""

import ssl
import sys
from pathlib import Path

internal_ca, target = Path(sys.argv[1]), Path(sys.argv[2])
paths = ssl.get_default_verify_paths()
system = next(
    (Path(p) for p in (paths.cafile, paths.openssl_cafile) if p and Path(p).is_file()), None
)
if system is None:
    raise SystemExit("No system CA bundle found in the image")
ca = internal_ca.read_text()
if "BEGIN CERTIFICATE" not in ca:
    raise SystemExit("Internal CA certificate is missing")
target.write_text(system.read_text().rstrip() + "\n" + ca)
print(f"trust bundle written: {target}")
