"""One durable media deliverable per delivery profile.

The complete MKV is the sole default media product. FLAC/SRT/ASS are verified
working sources, not automatically retained user exports. Retain lightweight
manifests and provenance. Never delete a source until a verified replacement
container has been atomically published and the receipt is sealed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from .archive_bundle_lifecycle import (
    OUTPUT, RECEIPT, _read_receipt, _signature, bundle_export_status,
)
from .builder import atomic_write_text

FLAC = "continuous.flac"
SRT = "HSR_Voice_Archive.srt"
ASS = "HSR_Voice_Archive.ass"
MEDIA_NAMES = (FLAC, SRT, ASS)
LEGACY_BLACK = "HSR_Voice_Archive_Black.mkv"


def _probe(path: Path) -> list[dict]:
    exe = shutil.which("ffprobe")
    if not exe:
        raise RuntimeError("Extracting an archive track requires FFprobe")
    process = subprocess.run(
        [exe, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    return json.loads(process.stdout)["streams"]


def _copy_track(source: Path, target: Path, *, map_arg: str, codec: str,
                fmt: str) -> None:
    if target.is_symlink() or source.is_symlink():
        raise ValueError("Unsafe symlink for delivery output")
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("Extracting an archive track requires FFmpeg")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".hsr-extract-", suffix=target.suffix, dir=target.parent)
    os.close(fd)
    tmp = Path(name)
    try:
        subprocess.run(
            [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
             "-map", map_arg, "-c", "copy", "-f", fmt, str(tmp)],
            check=True, capture_output=True, timeout=300,
        )
        tracks = _probe(tmp)
        if not tmp.stat().st_size or not any(x.get("codec_name") == codec for x in tracks):
            raise RuntimeError(f"Extracted track is not valid {codec}")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def extract_flac_from_mkv(output: Path, *, target: Path | None = None) -> Path:
    """Stream-copy the existing lossless audio; never rerun WAV -> FLAC."""
    mkv = output / OUTPUT
    receipt = _read_receipt(output)
    if (not receipt or _signature(mkv) != receipt.get("artifact")
            or not receipt.get("sealed")):
        raise ValueError("No verified sealed MKV is available for FLAC extraction")
    tracks = _probe(mkv)
    audio = [x for x in tracks if x.get("codec_type") == "audio"]
    if len(audio) != 1 or audio[0].get("codec_name") != "flac":
        raise ValueError("MKV must contain exactly one lossless FLAC track")
    destination = target or output / FLAC
    _copy_track(mkv, destination, map_arg="0:a:0", codec="flac", fmt="flac")
    return destination


def prepare_complete_sources(output: Path, *, source_language: str = "en",
                             target_language: str = "zh-CN",
                             render_config=None) -> None:
    """Restore missing working sources only when the prior MKV is verified.

    Existing edited SRT/ASS are not overwritten. Fresh captions are recreated
    from the project manifest and user overrides, then receipt-certified.
    """
    missing_audio = not (output / FLAC).is_file()
    missing_subtitles = any(not (output / name).is_file() for name in (SRT, ASS))
    if not missing_audio and not missing_subtitles:
        return
    status = bundle_export_status(output)
    if status["state"] != "current":
        raise ValueError("Cannot recover working tracks from an unverified/stale MKV")
    if missing_audio:
        extract_flac_from_mkv(output)
    if missing_subtitles:
        # Recreate the editable subtitle *source* from the project data rather
        # than treating transcoded MKV subtitles as authoritative text.
        from .subtitles import refresh_subtitle_artifacts_from_settings
        refresh_subtitle_artifacts_from_settings(
            output, source_language=source_language,
            target_language=target_language, generate_ass=True,
            render_config=render_config,
        )


def seal_complete_delivery(output: Path) -> None:
    """Seal the complete MKV receipt, then release only unchanged working files.

    Order matters: if the process dies after the receipt update, remaining
    work files can be removed on the next successful seal attempt. If it dies
    before the receipt update, the original source files are still available.
    """
    health = bundle_export_status(output)
    if health["state"] != "current":
        raise ValueError("Cannot discard working files before MKV verification")
    receipt = _read_receipt(output)
    if receipt is None:
        raise ValueError("Missing MKV publication receipt")
    for name, key in ((FLAC, "flac"), (ASS, "ass"), (SRT, "srt")):
        path = output / name
        current = _signature(path)
        expected = receipt["sources"].get(key)
        if current is not None and current != expected:
            raise ValueError(f"Refusing to delete modified source: {name}")
        if path.is_symlink():
            raise ValueError(f"Refusing to delete symlinked source: {name}")
    receipt["sealed"] = True
    atomic_write_text(output / RECEIPT, json.dumps(receipt, ensure_ascii=False, indent=2))
    for name in MEDIA_NAMES:
        path = output / name
        path.unlink(missing_ok=True)


def publish_flac_only(output: Path) -> Path:
    """Switch the final product to one FLAC, safely retiring the complete MKV.

    Extraction is lossless; the old container is removed only once the extracted
    FLAC has passed independent codec checks.
    """
    audio = output / FLAC
    if not audio.is_file():
        extract_flac_from_mkv(output, target=audio)
    streams = _probe(audio)
    if not audio.stat().st_size or len([s for s in streams if s.get("codec_type") == "audio"
                                           and s.get("codec_name") == "flac"]) != 1:
        raise ValueError("Single-FLAC output failed codec validation")
    for name in (SRT, ASS):
        candidate = output / name
        if candidate.is_symlink():
            raise ValueError("Unsafe subtitle path")
    # The former bundle and its publication receipt are now an obsolete
    # *project-owned* output. A failure before this line leaves them intact.
    (output / OUTPUT).unlink(missing_ok=True)
    (output / RECEIPT).unlink(missing_ok=True)
    for name in (SRT, ASS):
        (output / name).unlink(missing_ok=True)
    return audio
