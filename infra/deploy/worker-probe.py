"""Probe the main worker, including SQL startup and progress through its loop."""

import time
from pathlib import Path

if time.time() - Path("/tmp/worker-heartbeat").stat().st_mtime > 120:
    raise SystemExit(1)
