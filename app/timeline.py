from __future__ import annotations

import json
import math
import os
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable


def _non_negative_seconds(value: float, name: str) -> float:
    number = float(value)
    if number < 0:
        raise ValueError(f"{name} must be non-negative")
    return number


def resolve_timeline(
    rows: list[dict[str, object]],
    sample_rate: int,
    *,
    intro_gap: float = 5.0,
    same_group_gap: float = 0.40,
    group_gap: float = 1.20,
) -> dict[str, Any]:
    """Resolve voice and gap segments once, using integer PCM sample positions."""

    if not rows:
        raise ValueError("Timeline requires at least one voice entry")
    if sample_rate <= 0:
        raise ValueError("Timeline sample_rate must be positive")

    intro_samples = round(_non_negative_seconds(intro_gap, "intro_gap") * sample_rate)
    same_samples = round(
        _non_negative_seconds(same_group_gap, "same_group_gap") * sample_rate
    )
    group_samples = round(
        _non_negative_seconds(group_gap, "group_gap") * sample_rate
    )

    cursor = intro_samples
    segments: list[dict[str, object]] = []
    timings: list[dict[str, int]] = []
    if intro_samples:
        segments.append({
            "type": "gap",
            "role": "intro",
            "start_sample": 0,
            "end_sample": intro_samples,
            "duration_samples": intro_samples,
        })

    for position, row in enumerate(rows):
        frames = int(row["source_frames"])
        if frames <= 0:
            raise ValueError(
                f"Voice entry has no PCM frames: {row.get('filename', position)}"
            )
        start = cursor
        audio_end = start + frames
        if position + 1 < len(rows):
            boundary = rows[position + 1].get("group") != row.get("group")
            gap_role = "chapter" if boundary else "between_voice"
            gap_samples = group_samples if boundary else same_samples
            next_start = audio_end + gap_samples
            display_end = max(audio_end, next_start - max(1, round(0.001 * sample_rate)))
        else:
            gap_role = ""
            gap_samples = 0
            next_start = audio_end
            display_end = audio_end

        segments.append({
            "type": "voice",
            "entry_id": int(row.get("index", position + 1)),
            "filename": str(row.get("filename", "")),
            "group": str(row.get("group", "")),
            "start_sample": start,
            "end_sample": audio_end,
            "duration_samples": frames,
        })
        timings.append({
            "start_sample": start,
            "audio_end_sample": audio_end,
            "display_end_sample": display_end,
            "next_start_sample": next_start,
        })
        if gap_samples:
            segments.append({
                "type": "gap",
                "role": gap_role,
                "start_sample": audio_end,
                "end_sample": next_start,
                "duration_samples": gap_samples,
            })
        cursor = next_start

    for segment in segments:
        segment["start_seconds"] = segment["start_sample"] / sample_rate
        segment["end_seconds"] = segment["end_sample"] / sample_rate
        segment["duration_seconds"] = segment["duration_samples"] / sample_rate

    return {
        "schema_version": 1,
        "sample_rate": sample_rate,
        "intro_gap_seconds": intro_samples / sample_rate,
        "same_group_gap_seconds": same_samples / sample_rate,
        "group_gap_seconds": group_samples / sample_rate,
        "total_samples": cursor,
        "duration_seconds": cursor / sample_rate,
        "segments": segments,
        "entry_timings": timings,
    }


def _atomic_write(path: Path, text: str, encoding: str = "utf-8") -> None:
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


def write_resolved_timeline(
    entries: Iterable[object],
    report: dict[str, object],
    path: Path,
) -> dict[str, Any]:
    rows = []
    for entry in entries:
        rows.append({
            "index": getattr(entry, "index"),
            "filename": getattr(entry, "filename"),
            "group": getattr(entry, "group"),
            "source_frames": getattr(entry, "source_frames"),
            "start_sample": getattr(entry, "start_sample"),
            "audio_end_sample": getattr(entry, "audio_end_sample"),
            "display_end_sample": round(
                float(getattr(entry, "display_end_seconds"))
                * int(getattr(entry, "sample_rate"))
            ),
            "next_start_sample": getattr(entry, "next_start_sample"),
            "source_text": getattr(entry, "english"),
            "target_text": getattr(entry, "chinese"),
        })
    if not rows:
        raise ValueError("Cannot write an empty resolved timeline")

    sample_rate = int(report["sample_rate"])
    segments: list[dict[str, object]] = []
    previous_end = 0
    previous_group: str | None = None
    for row in rows:
        start = int(row["start_sample"])
        current_group = str(row["group"])
        if start > previous_end:
            role = (
                "intro"
                if previous_end == 0
                else "chapter"
                if previous_group is not None and current_group != previous_group
                else "between_voice"
            )
            segments.append({
                "type": "gap",
                "role": role,
                "start_sample": previous_end,
                "end_sample": start,
                "duration_samples": start - previous_end,
                "start_seconds": previous_end / sample_rate,
                "end_seconds": start / sample_rate,
                "duration_seconds": (start - previous_end) / sample_rate,
            })
        segments.append({
            "type": "voice",
            **row,
            "start_seconds": start / sample_rate,
            "end_seconds": int(row["audio_end_sample"]) / sample_rate,
            "duration_seconds": int(row["source_frames"]) / sample_rate,
        })
        previous_end = int(row["audio_end_sample"])
        previous_group = current_group

    payload = {
        "schema_version": 1,
        "sample_rate": sample_rate,
        "total_samples": int(report["total_samples"]),
        "duration_seconds": int(report["total_samples"]) / sample_rate,
        "intro_gap_seconds": float(report.get("intro_gap_seconds", 0.0)),
        "same_group_gap_seconds": float(report.get("same_group_gap_seconds", 0.0)),
        "group_gap_seconds": float(report.get("group_gap_seconds", 0.0)),
        "segments": segments,
    }
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{fraction:02d}"


ASS_PLAYRES_X = 1920
ASS_HORIZONTAL_MARGIN = 96
ASS_SOURCE_BASE_SIZE = 60
ASS_SOURCE_MIN_SIZE = 40
ASS_TARGET_BASE_SIZE = 68
ASS_TARGET_MIN_SIZE = 48


def _ass_text(value: object) -> str:
    return (
        str(value or "")
        .replace("\\", "／")
        .replace("{", "｛")
        .replace("}", "｝")
        .replace("\r", " ")
        .replace("\n", r"\N")
    )


def _ass_em_width(character: str) -> float:
    """Estimate glyph width in em without adding a font-metrics dependency."""

    if character.isspace():
        return 0.32
    if unicodedata.east_asian_width(character) in {"W", "F"}:
        return 1.0
    if character in "ilI.,'\`!|:;":
        return 0.30
    if character in "MW@#%&":
        return 0.90
    if character.isupper():
        return 0.64
    if character.isdigit():
        return 0.56
    return 0.54


def _estimated_ass_lines(value: object, font_size: int) -> int:
    """Estimate wrapped lines inside the fixed 1920x1080 ASS safe width."""

    text = (
        str(value or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    if not text:
        return 1

    usable_pixels = ASS_PLAYRES_X - 2 * ASS_HORIZONTAL_MARGIN
    em_per_line = usable_pixels / max(1, font_size)
    lines = 0
    for explicit_line in text.split("\n"):
        width = sum(_ass_em_width(character) for character in explicit_line)
        lines += max(1, math.ceil(width / em_per_line))
    return lines


def _fit_ass_font_size(
    value: object,
    *,
    base_size: int,
    minimum_size: int,
    max_lines: int,
) -> int:
    """Shrink only long entries; short dialogue keeps the larger archive style."""

    for size in range(base_size, minimum_size - 1, -2):
        if _estimated_ass_lines(value, size) <= max_lines:
            return size
    return minimum_size


def _ass_dialogue(
    *,
    layer: int,
    start: str,
    end: str,
    style: str,
    text: str,
    font_size: int,
) -> str:
    return (
        f"Dialogue: {layer},{start},{end},{style},,0,0,0,,"
        f"{{\\fs{font_size}}}{text}"
    )


def render_ass(
    entries: Iterable[object],
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> str:
    # Bilingual archives deliberately use two independent regions:
    # source text is anchored at the top and grows downward, while Chinese is
    # anchored at the bottom and grows upward. This keeps long passages from
    # colliding around the screen centre and uses the black-video canvas.
    header = """[Script Info]
Title: HSR Voice Archive
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ArchiveSource,Noto Sans,60,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,0,8,96,96,110,1
Style: ArchiveTarget,汉仪旗黑,68,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,96,96,110,1
Style: ArchiveSourceOnly,Noto Sans,60,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,0,5,96,96,40,1
Style: ArchiveChinese,汉仪旗黑,68,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,0,5,96,96,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues: list[str] = []
    same_chinese = source_language == target_language == "zh-CN"
    for entry in entries:
        source_raw = str(getattr(entry, "english", "") or "").strip()
        target_raw = str(getattr(entry, "chinese", "") or "").strip()
        start = _ass_time(float(getattr(entry, "start_seconds")))
        end = _ass_time(float(getattr(entry, "display_end_seconds")))

        if same_chinese:
            single_raw = target_raw or source_raw
            if not single_raw:
                continue
            dialogues.append(
                _ass_dialogue(
                    layer=0,
                    start=start,
                    end=end,
                    style="ArchiveChinese",
                    text=_ass_text(single_raw),
                    font_size=_fit_ass_font_size(
                        single_raw,
                        base_size=ASS_TARGET_BASE_SIZE,
                        minimum_size=ASS_TARGET_MIN_SIZE,
                        max_lines=5,
                    ),
                )
            )
            continue

        if source_raw and target_raw:
            dialogues.append(
                _ass_dialogue(
                    layer=0,
                    start=start,
                    end=end,
                    style="ArchiveSource",
                    text=_ass_text(source_raw),
                    font_size=_fit_ass_font_size(
                        source_raw,
                        base_size=ASS_SOURCE_BASE_SIZE,
                        minimum_size=ASS_SOURCE_MIN_SIZE,
                        max_lines=5,
                    ),
                )
            )
            dialogues.append(
                _ass_dialogue(
                    layer=1,
                    start=start,
                    end=end,
                    style="ArchiveTarget",
                    text=_ass_text(target_raw),
                    font_size=_fit_ass_font_size(
                        target_raw,
                        base_size=ASS_TARGET_BASE_SIZE,
                        minimum_size=ASS_TARGET_MIN_SIZE,
                        max_lines=4,
                    ),
                )
            )
        elif target_raw:
            dialogues.append(
                _ass_dialogue(
                    layer=0,
                    start=start,
                    end=end,
                    style="ArchiveChinese",
                    text=_ass_text(target_raw),
                    font_size=_fit_ass_font_size(
                        target_raw,
                        base_size=ASS_TARGET_BASE_SIZE,
                        minimum_size=ASS_TARGET_MIN_SIZE,
                        max_lines=5,
                    ),
                )
            )
        elif source_raw:
            dialogues.append(
                _ass_dialogue(
                    layer=0,
                    start=start,
                    end=end,
                    style="ArchiveSourceOnly",
                    text=_ass_text(source_raw),
                    font_size=_fit_ass_font_size(
                        source_raw,
                        base_size=ASS_SOURCE_BASE_SIZE,
                        minimum_size=ASS_SOURCE_MIN_SIZE,
                        max_lines=6,
                    ),
                )
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


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, fraction = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{fraction:03d}"


def _srt_text(value: object) -> str:
    return (
        str(value or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


def render_srt(
    entries: Iterable[object],
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> str:
    """Render SRT from the same resolved sample positions used by ASS and FLAC."""
    same_chinese = source_language == target_language == "zh-CN"
    blocks: list[str] = []
    for entry in entries:
        source = _srt_text(getattr(entry, "english", ""))
        target = _srt_text(getattr(entry, "chinese", ""))
        if same_chinese:
            text = target or source
        elif source and target:
            text = f"{source}\n{target}"
        else:
            text = target or source
        if not text:
            continue
        start = _srt_time(float(getattr(entry, "start_seconds")))
        end = _srt_time(float(getattr(entry, "display_end_seconds")))
        blocks.append(f"{len(blocks) + 1}\n{start} --> {end}\n{text}\n")
    if not blocks:
        raise ValueError("SRT rendering produced no subtitle lines")
    return "\n".join(blocks)


def write_srt(
    entries: Iterable[object],
    path: Path,
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
) -> None:
    _atomic_write(
        path,
        render_srt(
            entries,
            source_language=source_language,
            target_language=target_language,
        ),
        encoding="utf-8-sig",
    )
