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
    OUTPUT, RECEIPT, _read_receipt, _signature, _bundle_artifact_matches, bundle_export_status,
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


def _verify_primary_flac_decodes(source: Path) -> None:
    """Decode the entire primary FLAC stream, not only the stream headers.

    This is an explicit, potentially expensive pre-mutation gate for ASS-internal
    publication. Never claim an FFprobe stream listing proves PCM integrity.
    """
    if source.is_symlink() or not source.is_file():
        raise ValueError("Unsafe or absent primary audio source")
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("完整语音校验需要安装 FFmpeg；字幕及旧 MKV 均未改写")
    before = source.stat()
    command = [
        executable, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-xerror", "-i", str(source), "-map", "0:a:0",
        "-vn", "-sn", "-dn", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            check=False, timeout=1800,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(
            "完整 FLAC 解码校验无法完成；旧字幕和 MKV 均未改写"
        ) from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")[-350:]
        raise ValueError(
            "完整 FLAC 解码校验失败，音频可能含损坏帧；旧字幕和 MKV 均未改写："
            + detail
        )
    after = source.stat()
    identity = lambda stat: (
        stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
    )
    if source.is_symlink() or identity(before) != identity(after):
        raise ValueError("音源在完整解码检查期间发生变化；旧字幕和 MKV 均未改写")


def verify_internal_ass_audio_source(output: Path) -> dict:
    """Probe real audio before an explicit ASS-internal export changes receipts.

    A nonempty working FLAC is *not* proof of decodability. Likewise, the
    sealed-MKV receipt authenticates the file's identity, not the codec read
    needed for muxing. Any failure must be detected before rewriting captions.
    """
    from .archive_bundle_lifecycle import (
        bundle_audio_source_status, require_bound_voice_timeline,
    )

    require_bound_voice_timeline(output)
    status = bundle_audio_source_status(output)
    if not status["available"]:
        raise ValueError(str(status["reason"]))
    source = (output / FLAC if status["source"] == "independent-flac"
              else output / OUTPUT)
    try:
        streams = _probe(source)
    except (OSError, ValueError, KeyError, RuntimeError,
            subprocess.SubprocessError) as exc:
        raise ValueError(
            "音频来源无法通过 FFprobe 解码前检查："
            + source.name
            + "；旧字幕和 MKV 均未改写。请检查临时 FLAC 或封存 MKV"
        ) from exc
    audio_tracks = [row for row in streams
                    if row.get("codec_type") == "audio"]
    if not audio_tracks or audio_tracks[0].get("codec_name") != "flac":
        raise ValueError(
            "音频来源的第一条音轨不是预期的无损 FLAC："
            + source.name
            + "；旧字幕和 MKV 均未改写"
        )
    _verify_primary_flac_decodes(source)
    return status


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
    """Stream-copy default primary FLAC; chapter tracks remain independently selectable."""
    mkv = output / OUTPUT
    receipt = _read_receipt(output)
    if (not receipt or not _bundle_artifact_matches(mkv, receipt)
            or not receipt.get("sealed")):
        raise ValueError("No verified sealed MKV is available for FLAC extraction")
    from .archive_bundle_lifecycle import require_bound_voice_timeline
    require_bound_voice_timeline(output)
    tracks = _probe(mkv)
    audio = [x for x in tracks if x.get("codec_type") == "audio"]
    if not audio or audio[0].get("codec_name") != "flac":
        raise ValueError("MKV must contain a default primary lossless FLAC track")
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
    receipt = _read_receipt(output)
    if (not receipt or not receipt.get("sealed")
            or not _bundle_artifact_matches(output / OUTPUT, receipt)):
        raise ValueError("Cannot restore sources from an unverified MKV")
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
    # We are still inside preserve_published_archive, whose marker must
    # remain until commit/rollback. Exempt ONLY this owned transaction;
    # all MKV, subtitle, font and source-receipt evidence stays enforced.
    health = bundle_export_status(output, _owned_build=True)
    if health["state"] != "current":
        raise ValueError(
            "Cannot discard working files before MKV verification: "
            + str(health.get("message", "unknown verification state"))
        )
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
    # The old subtitle-free black-video output was produced by HSR and is now
    # superseded by the single verified full bundle.
    legacy = output / LEGACY_BLACK
    if legacy.is_symlink():
        raise ValueError("Unsafe historical black-MKV output path")
    legacy.unlink(missing_ok=True)


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
    for name in (SRT, ASS, LEGACY_BLACK):
        candidate = output / name
        if candidate.is_symlink():
            raise ValueError("Unsafe project-generated output path")
    # The former bundle and its publication receipt are now an obsolete
    # *project-owned* output. A failure before this line leaves them intact.
    receipt = _read_receipt(output)
    if receipt:
        # Preserve only the small replayable background configuration, never
        # the old MKV or the redundant FLAC track after profile transition.
        from .archive_bundle_lifecycle import PREFERENCES
        atomic_write_text(output / PREFERENCES, json.dumps({
            "schema_version": 1,
            "background": receipt["background"],
            "attach_project_fonts": receipt["attach_project_fonts"],
        }, ensure_ascii=False, indent=2))
    (output / OUTPUT).unlink(missing_ok=True)
    (output / RECEIPT).unlink(missing_ok=True)
    for name in (SRT, ASS, LEGACY_BLACK):
        (output / name).unlink(missing_ok=True)
    return audio


def _comparable_stage_inputs(value: dict) -> dict:
    """Delivery representation never changes the underlying encoded voice."""
    ignored = {"generate_ass", "delivery_mode", "generate_chapter_flac"}
    return {k: v for k, v in value.items()
            if k not in ignored and k != "software_stage_version"}


def recover_previous_audio_report(state_dir: Path, current_inputs: dict,
                                  output: Path) -> dict | None:
    """Prove an existing FLAC or sealed MKV was made for the same audio inputs.

    Relies on the former scan/audio stage records and the verified MKV receipt;
    never trusts filename equality alone. The caller records a fresh audio
    artifact SHA after restoring the FLAC stream into a working file.
    """
    scan = state_dir / "stages/01_scan.json"
    audio = state_dir / "stages/06_audio_state.json"
    if not scan.is_file() or not audio.is_file():
        return None
    try:
        a = json.loads(scan.read_text("utf-8"))
        b = json.loads(audio.read_text("utf-8"))
        previous = a["payload"]["inputs"]
        if (_comparable_stage_inputs(previous)
                != _comparable_stage_inputs(current_inputs)
                or a["input_fingerprint"] != b["input_fingerprint"]
                or b["stage"] != "audio"
                or not isinstance(b["payload"]["report"], dict)):
            return None
        # A sealed MKV with the stored publication signature is proof that
        # the former audio stage was successfully muxed and validated.
        receipt = _read_receipt(output)
        sealed = bool(receipt and receipt.get("sealed")
                      and _bundle_artifact_matches(output / OUTPUT, receipt))
        if sealed:
            return b["payload"]["report"]
        # A FLAC-only archive can reuse its formerly certified exact bytes.
        source = output / FLAC
        if not source.is_file() or source.is_symlink():
            return None
        from .stages import _sha256_file
        return b["payload"]["report"] if any(
            row.get("path") == FLAC
            and row.get("size_bytes") == source.stat().st_size
            and row.get("sha256") == _sha256_file(source)
            for row in b.get("artifacts", [])
        ) else None
    except (OSError, KeyError, TypeError, ValueError):
        return None


def export_optional_track(output: Path, kind: Literal["flac", "srt", "ass"]) -> Path:
    """Explicit user export, not a default retained part of the complete archive.

    Downloads are materialized under exports/, never into the working source
    paths, so user exports cannot invalidate a verified MKV source receipt.
    """
    if kind not in {"flac", "srt", "ass"}:
        raise ValueError("Unsupported individual track export")
    audio = output / FLAC
    if kind == "flac" and audio.is_file() and not (output / OUTPUT).is_file():
        return audio
    receipt = _read_receipt(output)
    if (not receipt or not _bundle_artifact_matches(output / OUTPUT, receipt)):
        raise ValueError("Only a verified MKV can supply an independent export")
    tracks = _probe(output / OUTPUT)
    track_info = {
        "flac": ("0:a:0", "flac", "flac", FLAC),
        "ass": ("0:s:0", "ass", "ass", ASS),
        "srt": ("0:s:1", "subrip", "srt", SRT),
    }
    stream_map, codec, fmt, name = track_info[kind]
    expected = {"flac": ("audio", "flac"), "ass": ("subtitle", "ass"),
                "srt": ("subtitle", "subrip")}
    media_type, media_codec = expected[kind]
    streams = [row for row in tracks if row.get("codec_type") == media_type]
    index = 1 if kind == "srt" else 0
    if index >= len(streams) or streams[index].get("codec_name") != media_codec:
        raise ValueError("Requested track is not present in the verified MKV")
    export_dir = output / "exports"
    # Explicit exports must stay within their project. A symlinked exports/
    # folder otherwise redirects writes outside the selected project root.
    if export_dir.is_symlink():
        raise ValueError("Unsafe symlinked track export directory")
    exported = export_dir / name
    _copy_track(output / OUTPUT, exported, map_arg=stream_map, codec=codec, fmt=fmt)
    return exported
