"""Optional loop-progress signal used by Kubernetes worker probes."""

import os
from pathlib import Path


def heartbeat():
    if os.environ.get("WORKER_HEARTBEAT") == "true":
        Path("/tmp/worker-heartbeat").touch()
