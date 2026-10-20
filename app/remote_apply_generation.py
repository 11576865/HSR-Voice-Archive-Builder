"""Non-destructive publication of incremental audio source generations.

An active project configuration must never reference a directory that is
removed as part of preparing a replacement. The caller publishes a new
index separately, then switches the project config last.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_GENERATION = re.compile(r"combined-audio-[a-zA-Z0-9_-]+\\Z")


def publish_audio_generation(generated: Path, staged_audio: Path, generation: str) -> Path:
    """Install a unique, immutable directory without removing older generations."""
    if not _GENERATION.fullmatch(generation):
        raise ValueError("Invalid incremental audio generation name")
    if staged_audio.is_symlink() or not staged_audio.is_dir():
        raise ValueError("No safe staged audio directory to publish")
    versions = generated / "combined_audio_versions"
    versions.mkdir(parents=True, exist_ok=True)
    target = versions / generation
    if target.exists() or target.is_symlink():
        raise FileExistsError("Incremental audio generation already exists")
    os.replace(staged_audio, target)
    return target
