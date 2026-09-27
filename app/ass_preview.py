from __future__ import annotations

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
