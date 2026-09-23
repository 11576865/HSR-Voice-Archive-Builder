from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from .kinetic_motion import generate_kinetic_tags
from .layout_solver import solve_subtitle_layout
from .measure import measure_line_height, measure_text_width
from .safe_area import DEFAULT_SAFE_AREA, SafeArea


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


def format_karaoke_text(
    text: str,
    duration_sec: float,
    word_alignments: list[object] | None = None,
    is_cjk: bool = False,
) -> str:
    """Format subtitle text with ASS word-level karaoke timing tags (\\k<centiseconds>)."""
    if not text or duration_sec <= 0:
        return text

    total_cs = max(1, round(duration_sec * 100))

    if word_alignments:
        formatted_tokens: list[str] = []
        for item in word_alignments:
            word = ""
            cs = 0
            if isinstance(item, dict):
                word = str(item.get("word", ""))
                start = float(item.get("start", 0.0))
                end = float(item.get("end", 0.0))
                dur = float(item.get("duration", max(0.0, end - start)))
                cs = max(1, round(dur * 100))
            elif hasattr(item, "word"):
                word = str(getattr(item, "word", ""))
                start = float(getattr(item, "start", 0.0))
                end = float(getattr(item, "end", 0.0))
                dur = float(getattr(item, "duration", max(0.0, end - start)))
                cs = max(1, round(dur * 100))
            if word and cs > 0:
                formatted_tokens.append(f"{{\\k{cs}}}{word}")
        if formatted_tokens:
            return "".join(formatted_tokens)

    if is_cjk:
        tokens = list(text)
    else:
        words = text.split(" ")
        tokens = []
        for i, w in enumerate(words):
            if not w and i > 0:
                continue
            suffix = " " if i < len(words) - 1 else ""
            tokens.append(w + suffix)

    tokens = [t for t in tokens if t]
    if not tokens:
        return text

    weights = [max(1, len(t.strip())) for t in tokens]
    total_weight = sum(weights) or len(tokens)

    centiseconds: list[int] = []
    accumulated_cs = 0
    for idx, weight in enumerate(weights):
        if idx == len(weights) - 1:
            cs = max(1, total_cs - accumulated_cs)
        else:
            cs = max(1, round(total_cs * (weight / total_weight)))
            accumulated_cs += cs
        centiseconds.append(cs)

    formatted_parts: list[str] = []
    for token, cs in zip(tokens, centiseconds):
        formatted_parts.append(f"{{\\k{cs}}}{token}")

    return "".join(formatted_parts)


def generate_frosted_glass_card(
    lines: list[object],
    safe_area: SafeArea = DEFAULT_SAFE_AREA,
    pad_x: int = 24,
    pad_y: int = 12,
    radius: int = 16,
) -> tuple[str, tuple[int, int, int, int]] | None:
    """Generate ASS vector card drawing (\\p1) for a frosted glass backdrop behind text lines."""
    if not lines:
        return None

    pos0 = lines[0]
    font_size = getattr(pos0, "font_size", 42)
    lh = measure_line_height(font_size)
    total_height = len(lines) * lh

    max_text_w = max(
        measure_text_width(getattr(p, "text", str(p)), font_size)
        for p in lines
    )

    card_w = max_text_w + 2 * pad_x
    card_h = total_height + 2 * pad_y

    cx = getattr(pos0, "x", safe_area.canvas_width // 2)
    cy = getattr(pos0, "y", safe_area.canvas_height // 2)
    align = getattr(pos0, "alignment", 2)

    if align == 2:
        x1 = cx - card_w / 2.0
        x2 = cx + card_w / 2.0
        y1 = cy - total_height - pad_y
        y2 = cy + pad_y
    elif align == 8:
        x1 = cx - card_w / 2.0
        x2 = cx + card_w / 2.0
        y1 = cy - pad_y
        y2 = cy + total_height + pad_y
    else:
        x1 = cx - card_w / 2.0
        x2 = cx + card_w / 2.0
        y1 = cy - card_h / 2.0
        y2 = cy + card_h / 2.0

    x1 = max(safe_area.x_min, min(safe_area.x_max - 20, x1))
    x2 = min(safe_area.x_max, max(safe_area.x_min + 20, x2))
    y1 = max(safe_area.y_min, min(safe_area.y_max - 20, y1))
    y2 = min(safe_area.y_max, max(safe_area.y_min + 20, y2))

    x1_i, y1_i, x2_i, y2_i = round(x1), round(y1), round(x2), round(y2)
    w = x2_i - x1_i
    h = y2_i - y1_i
    if w <= 0 or h <= 0:
        return None

    r = min(radius, min(w, h) // 2)

    path = (
        f"m {x1_i + r} {y1_i} "
        f"l {x2_i - r} {y1_i} "
        f"b {x2_i} {y1_i} {x2_i} {y1_i + r} {x2_i} {y1_i + r} "
        f"l {x2_i} {y2_i - r} "
        f"b {x2_i} {y2_i} {x2_i - r} {y2_i} {x2_i - r} {y2_i} "
        f"l {x1_i + r} {y2_i} "
        f"b {x1_i} {y2_i} {x1_i} {y2_i - r} {x1_i} {y2_i - r} "
        f"l {x1_i} {y1_i + r} "
        f"b {x1_i} {y1_i} {x1_i + r} {y1_i} {x1_i + r} {y1_i}"
    )

    card_text = f"{{\\an7\\pos(0,0)\\p1\\1a&H60&\\1c&H101010&\\3a&HFF&\\4a&HFF&}}{path}\\p0"
    return card_text, (x1_i, y1_i, x2_i, y2_i)


def format_multiline_karaoke(
    lines: list[object],
    duration_sec: float,
    word_alignments: list[object] | None = None,
    is_cjk: bool = False,
) -> str:
    """Format multiline subtitle positions into karaoke-tagged ASS text."""
    if not lines:
        return ""
    if len(lines) == 1:
        return format_karaoke_text(
            getattr(lines[0], "text", str(lines[0])),
            duration_sec,
            word_alignments,
            is_cjk=is_cjk,
        )

    total_chars = sum(max(1, len(getattr(pos, "text", str(pos)))) for pos in lines)
    formatted_lines: list[str] = []
    accumulated_sec = 0.0
    for idx, pos in enumerate(lines):
        txt = getattr(pos, "text", str(pos))
        if idx == len(lines) - 1:
            line_dur = max(0.01, duration_sec - accumulated_sec)
        else:
            line_dur = max(0.01, duration_sec * (len(txt) / total_chars))
            accumulated_sec += line_dur
        formatted_lines.append(format_karaoke_text(txt, line_dur, None, is_cjk=is_cjk))
    return "\\N".join(formatted_lines)


def render_ass(
    entries: Iterable[object],
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
    overflow_report_path: Path | None = None,
    enable_karaoke: bool = False,
    enable_frosted_glass: bool = False,
    enable_multi_layer_outline: bool = False,
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
Style: CHS,汉仪旗黑,52,&H00FFFFFF,&H00A0A0A0,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3,2,8,192,192,54,1
Style: Primary,Noto Sans,42,&H00FFFFFF,&H00A0A0A0,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3,2,8,192,192,54,1
Style: Card,Noto Sans,10,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

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
        duration_sec = max(0.01, end_sec - start_sec)
        word_alignments = getattr(entry, "word_alignments", getattr(entry, "words", None))

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

        if enable_frosted_glass:
            if layout.primary_lines:
                res_pri = generate_frosted_glass_card(layout.primary_lines)
                if res_pri:
                    dialogues.append(
                        f"Dialogue: 0,{start_time},{end_time},Card,,0,0,0,,{res_pri[0]}"
                    )
            if layout.chs_lines:
                res_chs = generate_frosted_glass_card(layout.chs_lines)
                if res_chs:
                    dialogues.append(
                        f"Dialogue: 0,{start_time},{end_time},Card,,0,0,0,,{res_chs[0]}"
                    )

        if layout.primary_lines:
            pos0 = layout.primary_lines[0]
            if enable_multi_layer_outline:
                pri_plain = "\\N".join(getattr(pos, "text", str(pos)) for pos in layout.primary_lines)
                out_text = f"{{\\an{pos0.alignment}\\pos({pos0.x},{pos0.y})\\fs{pos0.font_size}\\bord6\\3c&H000000&\\3a&H40&\\shad3\\4c&H000000&}}{pri_plain}"
                dialogues.append(
                    f"Dialogue: 0,{start_time},{end_time},Primary,,0,0,0,,{out_text}"
                )

            if enable_karaoke:
                pri_text = format_multiline_karaoke(
                    layout.primary_lines,
                    duration_sec,
                    word_alignments=word_alignments if not source_language.startswith("zh") else None,
                    is_cjk=source_language.startswith("zh"),
                )
            else:
                pri_text = "\\N".join(pos.text for pos in layout.primary_lines)
            dialogue_text = f"{{\\an{pos0.alignment}\\pos({pos0.x},{pos0.y})\\fs{pos0.font_size}{motion_tags}}}{pri_text}"
            dialogues.append(
                f"Dialogue: 0,{start_time},{end_time},Primary,,0,0,0,,{dialogue_text}"
            )

        if layout.chs_lines:
            pos0 = layout.chs_lines[0]
            if enable_multi_layer_outline:
                chs_plain = "\\N".join(getattr(pos, "text", str(pos)) for pos in layout.chs_lines)
                out_text = f"{{\\an{pos0.alignment}\\pos({pos0.x},{pos0.y})\\fs{pos0.font_size}\\bord6\\3c&H000000&\\3a&H40&\\shad3\\4c&H000000&}}{chs_plain}"
                dialogues.append(
                    f"Dialogue: 1,{start_time},{end_time},CHS,,0,0,0,,{out_text}"
                )

            if enable_karaoke:
                chs_text = format_multiline_karaoke(
                    layout.chs_lines,
                    duration_sec,
                    word_alignments=word_alignments if source_language.startswith("zh") else None,
                    is_cjk=True,
                )
            else:
                chs_text = "\\N".join(pos.text for pos in layout.chs_lines)
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
    enable_karaoke: bool = False,
    enable_frosted_glass: bool = False,
    enable_multi_layer_outline: bool = False,
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
            enable_karaoke=enable_karaoke,
            enable_frosted_glass=enable_frosted_glass,
            enable_multi_layer_outline=enable_multi_layer_outline,
            enable_kinetic=enable_kinetic,
        ),
        encoding="utf-8-sig",
    )
