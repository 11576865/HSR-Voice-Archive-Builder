from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable

from .layout_solver import solve_subtitle_layout


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{fraction:02d}"


def _clean_ass_text(value: object) -> str:
    return (
        str(value or "")
        .replace("\\", "／")
        .replace("{", "｛")
        .replace("}", "｝")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _atomic_write(path: Path, text: str, encoding: str = "utf-8-sig") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def render_ass(
    entries: Iterable[object],
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> str:
    header = """[Script Info]
Title: HSR Voice Archive
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CHS,汉仪旗黑,52,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3,0,8,192,192,54,1
Style: Primary,Noto Sans,42,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3,0,8,192,192,54,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues: list[str] = []

    for entry in entries:
        raw_source = getattr(entry, "english", "")
        raw_target = getattr(entry, "chinese", "")
        clean_source = _clean_ass_text(raw_source)
        clean_target = _clean_ass_text(raw_target)

        if not clean_source and not clean_target:
            continue

        start_time = _ass_time(float(getattr(entry, "start_seconds", 0.0)))
        end_time = _ass_time(float(getattr(entry, "display_end_seconds", 0.0)))

        layout = solve_subtitle_layout(
            english_text=clean_source,
            chinese_text=clean_target,
            source_language=source_language,
            target_language=target_language,
        )

        for pos in layout.chs_lines:
            dialogue_text = f"{{\\an{pos.alignment}\\pos({pos.x},{pos.y})\\fs{pos.font_size}}}{pos.text}"
            dialogues.append(
                f"Dialogue: 1,{start_time},{end_time},CHS,,0,0,0,,{dialogue_text}"
            )

        for pos in layout.primary_lines:
            dialogue_text = f"{{\\an{pos.alignment}\\pos({pos.x},{pos.y})\\fs{pos.font_size}}}{pos.text}"
            dialogues.append(
                f"Dialogue: 0,{start_time},{end_time},Primary,,0,0,0,,{dialogue_text}"
            )

    if not dialogues:
        raise ValueError("ASS rendering produced no dialogue lines")

    return header + "\n".join(dialogues) + "\n"


def write_ass(
    entries: Iterable[object],
    path: Path,
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> None:
    _atomic_write(
        path,
        render_ass(
            entries,
            source_language=source_language,
            target_language=target_language,
        ),
        encoding="utf-8-sig",
    )
