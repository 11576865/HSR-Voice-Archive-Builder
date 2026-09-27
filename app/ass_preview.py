from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from subtitle_layout.ass_writer import write_ass
from subtitle_layout.config import SubtitleRenderConfig


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


def render_ass_preview_png(
    *,
    english_text: str,
    chinese_text: str,
    source_language: str,
    target_language: str,
    config: SubtitleRenderConfig,
    timestamp: float = 1.0,
) -> bytes:
    """Render one representative subtitle frame through FFmpeg/libass."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("实际 ASS 预览需要 FFmpeg")

    entry = PreviewEntry(
        english=str(english_text or ""),
        chinese=str(chinese_text or ""),
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
            "color=c=black:s=1920x1080:r=1:d=3",
            "-ss",
            f"{max(0.0, float(timestamp)):.3f}",
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
