from __future__ import annotations

import base64
import binascii
import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from subtitle_layout.ass_writer import write_ass
from subtitle_layout.config import SubtitleRenderConfig
from subtitle_layout.fonts import validate_ass_font_name


@dataclass
class PreviewEntry:
    english: str
    chinese: str
    start_seconds: float = 0.0
    display_end_seconds: float = 3.0
    word_alignments: list[object] | None = None


def _escape_subtitles_filter_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    return value.replace(":", "\\:").replace("'", "\\'")


def preview_config_from_payload(data: dict[str, object]) -> SubtitleRenderConfig:
    def as_bool(name: str, default: bool) -> bool:
        value = data.get(name, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    return SubtitleRenderConfig(
        enable_karaoke=as_bool("enable_karaoke", False),
        karaoke_mode=(str(data.get("karaoke_mode", "k") or "k").lower() if str(data.get("karaoke_mode", "k") or "k").lower() in {"k", "kf", "clip"} else "k"),
        enable_frosted_glass=as_bool("enable_translucent_card", False),
        enable_multi_layer_outline=as_bool("enable_multi_layer_outline", False),
        enable_kinetic=as_bool("enable_kinetic", True),
        fade_in_ms=max(0, min(2000, int(data.get("fade_in_ms", 200)))),
        fade_out_ms=max(0, min(2000, int(data.get("fade_out_ms", 200)))),
        use_audio_aware_fade=as_bool("use_audio_aware_fade", True),
        outline_width=max(0.0, min(12.0, float(data.get("outline_width", 3.0)))),
        shadow_depth=max(0.0, min(12.0, float(data.get("shadow_depth", 2.0)))),
        blur_radius=max(0.0, min(5.0, float(data.get("blur_radius", 0.0)))),
        card_opacity=max(0.0, min(1.0, float(data.get("card_opacity", 0.62)))),
        enable_soft_entry=as_bool("enable_soft_entry", False),
        soft_entry_scale_percent=max(
            90.0, min(100.0, float(data.get("soft_entry_scale_percent", 98.0)))
        ),
        soft_entry_blur=max(0.0, min(5.0, float(data.get("soft_entry_blur", 1.5)))),
        soft_entry_ms=max(0, min(1000, int(data.get("soft_entry_ms", 160)))),
        enable_archive_hud=as_bool("enable_archive_hud", False),
        archive_character=str(data.get("archive_character", "") or "").strip(),
        archive_hud_font_size=max(12, min(48, int(data.get("archive_hud_font_size", 22)))),
        archive_hud_opacity=max(0.1, min(1.0, float(data.get("archive_hud_opacity", 0.72)))),
        chs_font=validate_ass_font_name(data.get("chs_font", "汉仪旗黑")),
        primary_font=validate_ass_font_name(data.get("primary_font", "Noto Sans")),
        base_chs_size=max(12, min(120, int(data.get("base_chs_size", 52)))),
        base_primary_size=max(12, min(120, int(data.get("base_primary_size", 42)))),
        margin_horizontal_percent=max(
            0.0, min(0.40, float(data.get("margin_horizontal_percent", 0.03)))
        ),
        margin_vertical_percent=max(
            0.0, min(0.40, float(data.get("margin_vertical_percent", 0.05)))
        ),
        min_central_gap=max(0.0, min(200.0, float(data.get("min_central_gap", 20.0)))),
    )


def render_ass_preview_png(
    *,
    english_text: str,
    chinese_text: str,
    source_language: str,
    target_language: str,
    config: SubtitleRenderConfig,
    word_alignments: list[object] | None = None,
    duration_seconds: float = 3.0,
    timestamp: float = 1.0,
) -> bytes:
    """Render one representative subtitle frame through FFmpeg/libass."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("实际 ASS 预览需要 FFmpeg")

    duration = max(0.25, min(120.0, float(duration_seconds)))
    render_timestamp = max(0.0, min(duration - 0.01, float(timestamp)))
    entry = PreviewEntry(
        english=str(english_text or ""),
        chinese=str(chinese_text or ""),
        display_end_seconds=duration,
        word_alignments=word_alignments,
    )
    with tempfile.TemporaryDirectory(prefix="hsr-ass-preview-") as td:
        root = Path(td)
        ass_path = root / "preview.ass"
        png_path = root / "preview.png"
        write_ass(
            [entry],
            ass_path,
            source_language=source_language or "en",
            target_language=target_language or "zh-CN",
            config=config,
            overflow_report_path=root / "overflow.json",
        )
        escaped_ass = _escape_subtitles_filter_path(ass_path)
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=1920x1080:r=1:d={max(3.0, duration):.3f}",
            "-ss",
            f"{render_timestamp:.3f}",
            "-vf",
            f"subtitles='{escaped_ass}'",
            "-frames:v",
            "1",
            str(png_path),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if completed.returncode != 0 or not png_path.is_file() or png_path.stat().st_size == 0:
            detail = (completed.stderr or completed.stdout or "unknown FFmpeg error").strip()
            raise RuntimeError(f"FFmpeg/libass 预览失败: {detail[:1200]}")
        return png_path.read_bytes()


MAX_PREVIEW_SOURCE_BYTES = 4 * 1024 * 1024


def decode_preview_source_video(value: object) -> bytes | None:
    """Decode at most 4 MiB. Never accept arbitrary paths or URLs."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Source video must be a base64 string")
    if len(value) > 4 * ((MAX_PREVIEW_SOURCE_BYTES + 2) // 3):
        raise ValueError("Source video exceeds 4 MiB")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid source video encoding") from exc
    if not decoded or len(decoded) > MAX_PREVIEW_SOURCE_BYTES:
        raise ValueError("Source video must contain 1–4 MiB of bytes")
    return decoded


def _validate_preview_source(path: Path, start: float, duration: float) -> None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("Local footage preview requires FFprobe")
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Source video probe timed out") from exc
    if result.returncode != 0:
        raise ValueError("Source video could not be probed")
    try:
        obj = json.loads(result.stdout)
        streams = [s for s in obj.get("streams", []) if s.get("codec_type") == "video"]
        total = float(obj.get("format", {}).get("duration"))
        width = int(streams[0]["width"])
        height = int(streams[0]["height"])
    except (IndexError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Source lacks verified video duration/dimensions") from exc
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Source duration is invalid")
    if width < 16 or height < 16 or width > 3840 or height > 2160:
        raise ValueError("Source exceeds supported video dimensions (3840×2160)")
    if start + duration > total + 0.04:
        raise ValueError("Selected preview window extends beyond source")


def render_ass_preview_video(
    *,
    english_text: str,
    chinese_text: str,
    source_language: str,
    target_language: str,
    config: SubtitleRenderConfig,
    word_alignments: list[object] | None = None,
    duration_seconds: float = 3.0,
    container_format: str = "mp4",
    source_video_bytes: bytes | None = None,
    source_start_seconds: float = 0.0,
) -> bytes:
    """Render genuine ASS motion over black or an explicitly supplied local video.

    Upload input is ephemeral, never saved to a project. Audio is intentionally
    absent from this visual proof. Full production-media export is separate.
    """
    duration = float(duration_seconds)
    if not math.isfinite(duration) or not 0.5 <= duration <= 8.0:
        raise ValueError("Dynamic preview duration must be between 0.5 and 8 seconds")
    start = float(source_start_seconds)
    if not math.isfinite(start) or not 0 <= start <= 600:
        raise ValueError("Preview source offset must be between 0 and 600 seconds")
    if source_video_bytes is not None and (
        not isinstance(source_video_bytes, bytes)
        or not 0 < len(source_video_bytes) <= MAX_PREVIEW_SOURCE_BYTES
    ):
        raise ValueError("Source video must be 1–4 MiB of local bytes")
    fmt = str(container_format).lower()
    if fmt not in {"mp4", "webm"}:
        raise ValueError("Unsupported preview container: use mp4 or webm")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Dynamic ASS preview requires FFmpeg")
    entry = PreviewEntry(
        english=str(english_text or ""),
        chinese=str(chinese_text or ""),
        display_end_seconds=duration,
        word_alignments=word_alignments,
    )
    with tempfile.TemporaryDirectory(prefix="hsr-effect-video-") as td:
        root = Path(td)
        ass_path = root / "preview.ass"
        result_path = root / ("effect-preview." + fmt)
        write_ass(
            [entry], ass_path,
            source_language=source_language or "en",
            target_language=target_language or "zh-CN",
            config=config,
            overflow_report_path=root / "overflow.json",
        )
        escaped_ass = _escape_subtitles_filter_path(ass_path)
        if source_video_bytes is not None:
            source_path = root / "selected-local-footage"
            source_path.write_bytes(source_video_bytes)
            _validate_preview_source(source_path, start, duration)
            source_args = ["-ss", f"{start:.3f}", "-i", str(source_path)]
            # Normalize the user image without stretching its aspect ratio.
            video_filters = ("scale=960:540:force_original_aspect_ratio=decrease,"
                             "pad=960:540:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,")
        else:
            source_args = [
                "-f", "lavfi", "-i",
                f"color=c=black:s=960x540:r=24:d={duration:.3f}",
            ]
            video_filters = ""
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            *source_args, "-vf",
            video_filters + f"subtitles='{escaped_ass}':original_size=1920x1080,format=yuv420p",
            "-an",
        ]
        if fmt == "webm":
            command += ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8",
                        "-b:v", "1200k"]
        else:
            command += ["-c:v", "libx264", "-preset", "ultrafast",
                        "-crf", "22", "-movflags", "+faststart"]
        command += ["-t", f"{duration:.3f}", str(result_path)]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=50, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("FFmpeg/libass preview timed out") from exc
        if (completed.returncode != 0 or not result_path.is_file()
                or result_path.stat().st_size == 0):
            detail = (completed.stderr or completed.stdout or "unknown FFmpeg error").strip()
            raise RuntimeError(f"FFmpeg/libass preview failed: {detail[:1200]}")
        if result_path.stat().st_size > 16 * 1024 * 1024:
            raise RuntimeError("Dynamic preview exceeds 16 MiB output limit")
        return result_path.read_bytes()
