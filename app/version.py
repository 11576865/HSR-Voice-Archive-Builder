from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

APP_VERSION = "0.9-L"


@lru_cache(maxsize=1)
def runtime_version() -> str:
    """Return a user-visible version with the checked-out revision when available."""
    revision = os.environ.get("HSR_BUILD_REVISION", "").strip()
    if not revision:
        try:
            revision = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            revision = ""
    return f"{APP_VERSION}+{revision}" if revision else APP_VERSION
