from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from .kinetic_motion import generate_kinetic_tags
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
    overflow_report_path: Path | None = None,
    enable_kinetic: bool = True,
) -> str:
    header = """[Script Info]
Title: HSR Voice Archive
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CHS,汉仪旗黑,52,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3,0,8,192,192,54,1
Style: Primary,Noto Sans,42,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3,0,8,192,192,54,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues: list[str] = []
    overflow_records: list[dict[str, object]] = []

    for entry in entries:
        raw_source = getattr(entry, "english", getattr(entry, "source_text", ""))
        raw_target = getattr(entry, "chinese", getattr(entry, "target_text", ""))
        clean_source = _clean_ass_text(raw_source)
        clean_target = _clean_ass_text(raw_target)

        if not clean_source and not clean_target:
            continue

        start_sec = float(getattr(entry, "start_seconds", getattr(entry, "start", 0.0)))
        end_sec = float(getattr(entry, "display_end_seconds", getattr(entry, "end", 0.0)))
        start_time = _ass_time(start_sec)
        end_time = _ass_time(end_sec)
        duration_sec = max(0.1, end_sec - start_sec)

        layout = solve_subtitle_layout(
            english_text=clean_source,
            chinese_text=clean_target,
            source_language=source_language,
            target_language=target_language,
        )

        if layout.failed:
            subtitle_id = str(getattr(entry, "index", getattr(entry, "id", getattr(entry, "filename", ""))))
            overflow_records.append({
                "subtitle_id": subtitle_id,
                "time_range": {
                    "start": start_time,
                    "end": end_time,
                },
                "source_text": clean_source,
                "final_chs": clean_target,
                "scale_attempts": layout.scale_attempts,
                "failed_condition": layout.failed_condition,
            })
            continue

        motion_tags = generate_kinetic_tags(duration_sec) if enable_kinetic else ""

        if layout.primary_lines:
            pri_text = "\\N".join(pos.text for pos in layout.primary_lines)
            pos0 = layout.primary_lines[0]
            dialogue_text = f"{{\\an{pos0.alignment}\\pos({pos0.x},{pos0.y})\\fs{pos0.font_size}{motion_tags}}}{pri_text}"
            dialogues.append(
                f"Dialogue: 0,{start_time},{end_time},Primary,,0,0,0,,{dialogue_text}"
            )

        if layout.chs_lines:
            chs_text = "\\N".join(pos.text for pos in layout.chs_lines)
            pos0 = layout.chs_lines[0]
            dialogue_text = f"{{\\an{pos0.alignment}\\pos({pos0.x},{pos0.y})\\fs{pos0.font_size}{motion_tags}}}{chs_text}"
            dialogues.append(
                f"Dialogue: 1,{start_time},{end_time},CHS,,0,0,0,,{dialogue_text}"
            )

    if overflow_report_path is not None:
        _atomic_write(
            overflow_report_path,
            json.dumps(overflow_records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if not dialogues:
        if overflow_records:
            raise ValueError(
                f"ASS rendering failed for all entries ({len(overflow_records)} overflow errors). "
                f"See {overflow_report_path} for details."
            )
        raise ValueError("ASS rendering produced no dialogue lines")

    return header + "\n".join(dialogues) + "\n"


def write_ass(
    entries: Iterable[object],
    path: Path,
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
    overflow_report_path: Path | None = None,
    enable_kinetic: bool = True,
) -> None:
    report_path = overflow_report_path or (path.parent / "ass_layout_overflow_report.json")
    _atomic_write(
        path,
        render_ass(
            entries,
            source_language=source_language,
            target_language=target_language,
            overflow_report_path=report_path,
            enable_kinetic=enable_kinetic,
        ),
        encoding="utf-8-sig",
    )
