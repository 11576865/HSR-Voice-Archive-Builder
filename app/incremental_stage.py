"""One source-member-preserving incremental stage for FastAPI and Termux.

Source filenames are not globally unique: Quick Mode explicitly supports two
different chapter members having the same WAV basename. Never flatten them.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .builder import ensure_dir_or_extract
from .huggingface_audio import validated_incremental_files


def prepare_incremental_audio_stage(
    source: Path,
    working_root: Path,
    download: dict[str, Any],
    incoming: Path,
) -> Path:
    """Stage a new immutable generation, preserving old WAV/LAB member paths.

    Only fully verified current-operation downloads may enter the new source.
    A new flat WAV basename already present anywhere in the source would be
    ambiguous and must not silently replace either of its old members.
    Publication and project switching are handled by the caller.
    """
    fresh = validated_incremental_files(download, incoming)
    if not fresh:
        raise ValueError("No verified incremental audio files to publish")

    if source.is_symlink():
        raise ValueError("Incremental source must not be a symbolic link")
    extracted = ensure_dir_or_extract(source, working_root, "existing")
    if extracted.is_symlink() or not extracted.is_dir():
        raise ValueError("Extracted source root is not a safe directory")
    stage = working_root / "combined"
    stage.mkdir()

    old_names: set[str] = set()
    for member in sorted(extracted.rglob("*")):
        if member.is_symlink():
            raise ValueError(f"Source package contains unsafe symbolic link: {member}")
        if not member.is_file() or member.suffix.casefold() not in {".wav", ".lab"}:
            continue
        rel = member.relative_to(extracted)
        # Keep same-member Japanese/Chinese LAB files: the builder reads
        # these by chapter-relative path for non-English source text.
        # Dropping LAB files changes text provenance after incrementals.
        if member.suffix.casefold() == ".wav":
            old_names.add(member.name.casefold())
        out = stage / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(member, out)

    new_names = [audio.name.casefold() for audio in fresh]
    if len(new_names) != len(set(new_names)):
        raise ValueError("Incremental WAV basenames are not unique")
    duplicates = old_names.intersection(new_names)
    if duplicates:
        raise ValueError(
            "New audio filename already exists in the original package: "
            + ", ".join(sorted(duplicates)[:8])
            + "; refusing an ambiguous overwrite"
        )
    for audio in fresh:
        shutil.copy2(audio, stage / audio.name)
    return stage
