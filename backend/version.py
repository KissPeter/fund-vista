"""Backend build stamp: which commit answers a request.

Source of truth is env ``FUNDVISTA_VERSION`` (the Mac image bakes it in at
build time; FastAPI Cloud gets it as an app env var). Without it, a git
checkout answers ``git rev-parse --short=7 HEAD``; otherwise ``dev``.
``FUNDVISTA_BUILT`` (ISO time) is optional.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

_SHA_LEN = 7


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", f"--short={_SHA_LEN}", "HEAD"],
            cwd=Path(__file__).resolve().parent, capture_output=True,
            text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else None


@lru_cache(maxsize=1)
def backend_version() -> str:
    env = os.environ.get("FUNDVISTA_VERSION", "").strip()
    if env:
        return env[:_SHA_LEN] if len(env) >= 40 else env
    return _git_sha() or "dev"


def backend_built() -> str | None:
    return os.environ.get("FUNDVISTA_BUILT", "").strip() or None
