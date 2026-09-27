from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .config import SubtitleRenderConfig
from .fonts import resolve_font_path, validate_ass_font_name
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


def _normalized_alignment_text(value: str) -> str:
    return "".join(str(value or "").split())


def _validated_word_alignment_details(
    text: str,
    duration_sec: float,
    word_alignments: list[object] | None,
) -> list[tuple[str, float, float, float]] | None:
    if not text or duration_sec <= 0 or not word_alignments:
        return None

    tokens: list[tuple[str, float, float, float]] = []
    previous_end = 0.0
    covered = ""
    for item in word_alignments:
        try:
            if isinstance(item, dict):
                word = str(item.get("word", ""))
                start = float(item.get("start", 0.0))
                end = float(item.get("end", 0.0))
                duration = float(item.get("duration", max(0.0, end - start)))
            elif hasattr(item, "word"):
                word = str(getattr(item, "word", ""))
                start = float(getattr(item, "start", 0.0))
                end = float(getattr(item, "end", 0.0))
                duration = float(getattr(item, "duration", max(0.0, end - start)))
            else:
                return None
        except (TypeError, ValueError):
            return None

        if not word or start < -0.001 or end <= start or duration <= 0:
            return None
        if start + 0.02 < previous_end:
            return None
        if end > duration_sec + 0.25:
            return None
        previous_end = end
        covered += word
        tokens.append((word, start, end, duration))

    if _normalized_alignment_text(covered) != _normalized_alignment_text(text):
        return None
    return tokens


def _validated_word_alignments(
    text: str,
    duration_sec: float,
    word_alignments: list[object] | None,
) -> list[tuple[str, float]] | None:
    details = _validated_word_alignment_details(text, duration_sec, word_alignments)
    if not details:
        return None
    return [(word, duration) for word, _start, _end, duration in details]


def format_karaoke_text(
    text: str,
    duration_sec: float,
    word_alignments: list[object] | None = None,
    is_cjk: bool = False,
    mode: str = "k",
) -> str:
    """Apply ASS karaoke tags only for complete, monotonic word-level timing."""
    validated = _validated_word_alignments(text, duration_sec, word_alignments)
    if not validated:
        return text
    tag = "kf" if str(mode).lower() == "kf" else "k"
    return "".join(
        f"{{\\{tag}{max(1, round(duration * 100))}}}{word}"
        for word, duration in validated
    )


def generate_clip_karaoke_overlay(
    line: object,
    duration_sec: float,
    word_alignments: list[object] | None,
    *,
    font_path: str | None = None,
) -> str | None:
    """Build a progressive rectangular clip overlay from verified word timing.

    This is intentionally limited to one rendered line. Multi-line subtitles
    fall back to ordinary text instead of inventing a sweep path.
    """
    text = getattr(line, "text", str(line))
    details = _validated_word_alignment_details(text, duration_sec, word_alignments)
    if not details:
        return None

    font_size = int(getattr(line, "font_size", 42))
    center_x = float(getattr(line, "x", 0))
    anchor_y = float(getattr(line, "y", 0))
    alignment = int(getattr(line, "alignment", 2))
    text_width = max(1.0, measure_text_width(text, font_size, font_path))
    line_height = max(1.0, measure_line_height(font_size))
    x1 = round(center_x - text_width / 2.0)
    x2 = round(center_x + text_width / 2.0)
    if alignment == 2:
        y1 = round(anchor_y - line_height - 4)
        y2 = round(anchor_y + 4)
    elif alignment == 8:
        y1 = round(anchor_y - 4)
        y2 = round(anchor_y + line_height + 4)
    else:
        y1 = round(anchor_y - line_height / 2.0 - 4)
        y2 = round(anchor_y + line_height / 2.0 + 4)
    if x2 <= x1 or y2 <= y1:
        return None

    widths = [
        max(1.0, measure_text_width(word, font_size, font_path))
        for word, _start, _end, _duration in details
    ]
    total_width = sum(widths)
    if total_width <= 0:
        return None

    tags = [f"\\clip({x1},{y1},{x1},{y2})"]
    covered_width = 0.0
    for (word, start, end, _word_duration), width in zip(details, widths, strict=True):
        start_ms = max(0, round(start * 1000))
        end_ms = max(start_ms + 1, round(end * 1000))
        covered_width += width
        reveal_x = x1 + round((x2 - x1) * min(1.0, covered_width / total_width))
        tags.append(
            f"\\t({start_ms},{end_ms},\\clip({x1},{y1},{reveal_x},{y2}))"
        )
    return "".join(tags)


def _fmt_num(value: float) -> str:
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _ass_alpha_for_opacity(opacity: float) -> str:
    clamped = max(0.0, min(1.0, float(opacity)))
    # Floor preserves the historical 62% default as &H60 while still
    # mapping 0%/100% opacity to FF/00 exactly.
    alpha = int((1.0 - clamped) * 255)
    return f"{alpha:02X}"


def _soft_entry_tags(cfg: SubtitleRenderConfig, duration_sec: float) -> str:
    if not cfg.enable_soft_entry:
        return ""
    duration_ms = max(100, int(round(duration_sec * 1000)))
    entry_ms = min(max(0, int(cfg.soft_entry_ms)), max(0, duration_ms // 2))
    if entry_ms <= 0:
        return ""

    start_scale = max(90.0, min(100.0, float(cfg.soft_entry_scale_percent)))
    base_blur = max(0.0, min(5.0, float(cfg.blur_radius)))
    start_blur = max(base_blur, max(0.0, min(5.0, float(cfg.soft_entry_blur))))

    tags = (
        f"\\fscx{_fmt_num(start_scale)}"
        f"\\fscy{_fmt_num(start_scale)}"
        f"\\blur{_fmt_num(start_blur)}"
        f"\\t(0,{entry_ms},"
        f"\\fscx100\\fscy100\\blur{_fmt_num(base_blur)})"
    )
    return tags


def generate_frosted_glass_card(
    lines: list[object],
    safe_area: SafeArea = DEFAULT_SAFE_AREA,
    font_path: str | None = None,
    pad_x: int = 24,
    pad_y: int = 12,
    radius: int = 16,
    opacity: float = 0.62,
) -> tuple[str, tuple[int, int, int, int]] | None:
    """Generate a translucent ASS vector backdrop card (no blur is applied)."""
    if not lines:
        return None

    pos0 = lines[0]
    font_size = getattr(pos0, "font_size", 42)
    lh = measure_line_height(font_size)
    total_height = len(lines) * lh

    max_text_w = max(
        measure_text_width(getattr(p, "text", str(p)), font_size, font_path)
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

    alpha = _ass_alpha_for_opacity(opacity)
    card_text = f"{{\\an7\\pos(0,0)\\p1\\1a&H{alpha}&\\1c&H101010&\\3a&HFF&\\4a&HFF&}}{path}\\p0"
    return card_text, (x1_i, y1_i, x2_i, y2_i)


def format_multiline_karaoke(
    lines: list[object],
    duration_sec: float,
    word_alignments: list[object] | None = None,
    is_cjk: bool = False,
    mode: str = "k",
) -> str:
    """Apply karaoke only when timings can be mapped without inventing timing."""
    if not lines:
        return ""
    plain = "\\N".join(getattr(pos, "text", str(pos)) for pos in lines)
    if not word_alignments or len(lines) != 1:
        return plain
    return format_karaoke_text(
        getattr(lines[0], "text", str(lines[0])),
        duration_sec,
        word_alignments,
        is_cjk=is_cjk,
        mode=mode,
    )


def render_ass(
    entries: Iterable[object],
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
    overflow_report_path: Path | None = None,
    config: SubtitleRenderConfig | None = None,
    enable_karaoke: bool | None = None,
    enable_frosted_glass: bool | None = None,
    enable_multi_layer_outline: bool | None = None,
    enable_kinetic: bool | None = None,
    kinetic_options: dict[str, Any] | None = None,
) -> str:
    cfg = config or SubtitleRenderConfig()
    chs_font = validate_ass_font_name(cfg.chs_font)
    primary_font = validate_ass_font_name(cfg.primary_font)
    chs_font_path = resolve_font_path(chs_font)
    primary_font_path = resolve_font_path(primary_font)
    margin_h = round(1920 * max(0.0, min(0.40, float(cfg.margin_horizontal_percent))))
    margin_v = round(1080 * max(0.0, min(0.40, float(cfg.margin_vertical_percent))))
    header = f"""[Script Info]
Title: HSR Voice Archive
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CHS,{chs_font},{int(cfg.base_chs_size)},&H00FFFFFF,&H00A0A0A0,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,{_fmt_num(cfg.outline_width)},{_fmt_num(cfg.shadow_depth)},8,{margin_h},{margin_h},{margin_v},1
Style: Primary,{primary_font},{int(cfg.base_primary_size)},&H00FFFFFF,&H00A0A0A0,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,{_fmt_num(cfg.outline_width)},{_fmt_num(cfg.shadow_depth)},8,{margin_h},{margin_h},{margin_v},1
Style: Card,{primary_font},10,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Archive,{primary_font},{int(cfg.archive_hud_font_size)},&H{_ass_alpha_for_opacity(cfg.archive_hud_opacity)}FFFFFF,&HFFFFFFFF,&H80000000,&HFF000000,0,0,0,0,100,100,0,0,1,1,0,7,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    use_karaoke = cfg.enable_karaoke if enable_karaoke is None else enable_karaoke
    karaoke_mode = str(getattr(cfg, "karaoke_mode", "k") or "k").lower()
    if karaoke_mode not in {"k", "kf", "clip"}:
        karaoke_mode = "k"
    use_frosted_glass = cfg.enable_frosted_glass if enable_frosted_glass is None else enable_frosted_glass
    use_multi_layer_outline = cfg.enable_multi_layer_outline if enable_multi_layer_outline is None else enable_multi_layer_outline
    use_kinetic = cfg.enable_kinetic if enable_kinetic is None else enable_kinetic
    k_opts = dict(cfg.kinetic_options)
    if kinetic_options is not None:
        k_opts.update(kinetic_options)

    margin_h_pct = max(0.0, min(0.40, float(cfg.margin_horizontal_percent)))
    margin_v_pct = max(0.0, min(0.40, float(cfg.margin_vertical_percent)))
    safe_area = SafeArea(
        canvas_width=1920,
        canvas_height=1080,
        margin_left_percent=margin_h_pct,
        margin_right_percent=margin_h_pct,
        margin_top_percent=margin_v_pct,
        margin_bottom_percent=margin_v_pct,
    )

    dialogues: list[str] = []
    overflow_records: list[dict[str, object]] = []

    prev_end_sec: float | None = None

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
        voice_gap_sec = getattr(entry, "voice_gap_seconds", getattr(entry, "voice_gap", None))
        if voice_gap_sec is not None:
            voice_gap_sec = float(voice_gap_sec)

        layout = solve_subtitle_layout(
            english_text=clean_source,
            chinese_text=clean_target,
            source_language=source_language,
            target_language=target_language,
            base_chs_size=int(cfg.base_chs_size),
            base_primary_size=int(cfg.base_primary_size),
            safe_area=safe_area,
            min_central_gap=float(cfg.min_central_gap),
            chs_font_path=chs_font_path,
            primary_font_path=primary_font_path,
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

        # Project 2 Pre-roll audio-aware fade calculation
        actual_start_sec = start_sec
        fade_in_ms = cfg.fade_in_ms
        fade_out_ms = cfg.fade_out_ms

        if use_kinetic and cfg.use_audio_aware_fade and prev_end_sec is not None:
            delta_t_sec = start_sec - prev_end_sec
            if delta_t_sec > 0:
                pre_roll_sec = min(delta_t_sec / 2.0, fade_in_ms / 1000.0)
                actual_start_sec = max(prev_end_sec, start_sec - pre_roll_sec)

        start_time = _ass_time(actual_start_sec)
        duration_sec = max(0.01, end_sec - actual_start_sec)
        prev_end_sec = end_sec

        if cfg.enable_archive_hud:
            character = _clean_ass_text(
                getattr(entry, "character", "") or cfg.archive_character
            ).strip()
            chapter = _clean_ass_text(getattr(entry, "group", "")).strip()
            entry_id = _clean_ass_text(
                getattr(entry, "index", getattr(entry, "id", ""))
            ).strip()
            hud_parts = []
            if character:
                hud_parts.append(character.upper())
            if chapter:
                hud_parts.append(chapter)
            if entry_id:
                hud_parts.append(f"#{entry_id}")
            if hud_parts:
                hud_text = " · ".join(hud_parts)
                hud_x = round(safe_area.x_min + 10)
                hud_y = round(safe_area.y_min + 8)
                dialogues.append(
                    f"Dialogue: 5,{start_time},{end_time},Archive,,0,0,0,,"
                    f"{{\\an7\\pos({hud_x},{hud_y})}}{hud_text}"
                )

        if use_frosted_glass:
            if layout.primary_lines:
                res_pri = generate_frosted_glass_card(layout.primary_lines, safe_area=safe_area, font_path=primary_font_path, opacity=cfg.card_opacity)
                if res_pri:
                    card_tag = res_pri[0]
                    if use_kinetic:
                        card_tag = card_tag.replace("{\\an7", f"{{\\an7\\fad({fade_in_ms},{fade_out_ms})", 1)
                    dialogues.append(
                        f"Dialogue: 0,{start_time},{end_time},Card,,0,0,0,,{card_tag}"
                    )
            if layout.chs_lines:
                res_chs = generate_frosted_glass_card(layout.chs_lines, safe_area=safe_area, font_path=chs_font_path, opacity=cfg.card_opacity)
                if res_chs:
                    card_tag = res_chs[0]
                    if use_kinetic:
                        card_tag = card_tag.replace("{\\an7", f"{{\\an7\\fad({fade_in_ms},{fade_out_ms})", 1)
                    dialogues.append(
                        f"Dialogue: 0,{start_time},{end_time},Card,,0,0,0,,{card_tag}"
                    )

        if layout.primary_lines:
            pos0 = layout.primary_lines[0]
            if use_kinetic:
                pri_motion_tags = generate_kinetic_tags(
                    duration_sec,
                    x=pos0.x,
                    y=pos0.y,
                    fade_in_ms=fade_in_ms,
                    fade_out_ms=fade_out_ms,
                    entry_y_offset=k_opts.get("primary_entry_y_offset", 0),
                    voice_gap_seconds=voice_gap_sec if cfg.use_audio_aware_fade else None,
                    **{k: v for k, v in k_opts.items() if k not in ("primary_entry_y_offset", "chs_entry_y_offset")},
                )
                pos_prefix = f"{{\\an{pos0.alignment}{pri_motion_tags}"
            else:
                pos_prefix = f"{{\\an{pos0.alignment}\\pos({pos0.x},{pos0.y})"

            soft_entry_tags = _soft_entry_tags(cfg, duration_sec)
            base_blur_tag = (
                f"\\blur{_fmt_num(cfg.blur_radius)}"
                if float(cfg.blur_radius) > 0 and not soft_entry_tags
                else ""
            )
            if use_multi_layer_outline:
                pri_plain = "\\N".join(getattr(pos, "text", str(pos)) for pos in layout.primary_lines)
                outer_bord = max(float(cfg.outline_width) + 3.0, float(cfg.outline_width) * 1.8)
                outer_shadow = max(float(cfg.shadow_depth), 3.0)
                outer_blur = max(float(cfg.blur_radius), 0.6)
                out_text = (
                    f"{pos_prefix}\\fs{pos0.font_size}"
                    f"\\bord{_fmt_num(outer_bord)}\\3c&H000000&\\3a&H40&"
                    f"\\shad{_fmt_num(outer_shadow)}\\4c&H000000&"
                    f"\\blur{_fmt_num(outer_blur)}}}{pri_plain}"
                )
                dialogues.append(
                    f"Dialogue: 0,{start_time},{end_time},Primary,,0,0,0,,{out_text}"
                )

            pri_alignments = word_alignments if not source_language.startswith("zh") else None
            if use_karaoke and karaoke_mode in {"k", "kf"}:
                pri_text = format_multiline_karaoke(
                    layout.primary_lines,
                    duration_sec,
                    word_alignments=pri_alignments,
                    is_cjk=source_language.startswith("zh"),
                    mode=karaoke_mode,
                )
            else:
                pri_text = "\\N".join(pos.text for pos in layout.primary_lines)
            dialogue_text = f"{pos_prefix}\\fs{pos0.font_size}{soft_entry_tags}{base_blur_tag}}}{pri_text}"
            dialogues.append(
                f"Dialogue: 0,{start_time},{end_time},Primary,,0,0,0,,{dialogue_text}"
            )
            if use_karaoke and karaoke_mode == "clip" and len(layout.primary_lines) == 1:
                clip_tags = generate_clip_karaoke_overlay(
                    layout.primary_lines[0],
                    duration_sec,
                    pri_alignments,
                    font_path=primary_font_path,
                )
                if clip_tags:
                    clip_text = getattr(layout.primary_lines[0], "text", str(layout.primary_lines[0]))
                    overlay = (
                        f"{pos_prefix}\\fs{pos0.font_size}\\1c&H00E8FF&"
                        f"\\bord0\\shad0{clip_tags}}}{clip_text}"
                    )
                    dialogues.append(
                        f"Dialogue: 3,{start_time},{end_time},Primary,,0,0,0,,{overlay}"
                    )

        if layout.chs_lines:
            pos0 = layout.chs_lines[0]
            if use_kinetic:
                chs_motion_tags = generate_kinetic_tags(
                    duration_sec,
                    x=pos0.x,
                    y=pos0.y,
                    fade_in_ms=fade_in_ms,
                    fade_out_ms=fade_out_ms,
                    entry_y_offset=k_opts.get("chs_entry_y_offset", 0),
                    voice_gap_seconds=voice_gap_sec if cfg.use_audio_aware_fade else None,
                    **{k: v for k, v in k_opts.items() if k not in ("primary_entry_y_offset", "chs_entry_y_offset")},
                )
                pos_prefix = f"{{\\an{pos0.alignment}{chs_motion_tags}"
            else:
                pos_prefix = f"{{\\an{pos0.alignment}\\pos({pos0.x},{pos0.y})"

            soft_entry_tags = _soft_entry_tags(cfg, duration_sec)
            base_blur_tag = (
                f"\\blur{_fmt_num(cfg.blur_radius)}"
                if float(cfg.blur_radius) > 0 and not soft_entry_tags
                else ""
            )
            if use_multi_layer_outline:
                chs_plain = "\\N".join(getattr(pos, "text", str(pos)) for pos in layout.chs_lines)
                outer_bord = max(float(cfg.outline_width) + 3.0, float(cfg.outline_width) * 1.8)
                outer_shadow = max(float(cfg.shadow_depth), 3.0)
                outer_blur = max(float(cfg.blur_radius), 0.6)
                out_text = (
                    f"{pos_prefix}\\fs{pos0.font_size}"
                    f"\\bord{_fmt_num(outer_bord)}\\3c&H000000&\\3a&H40&"
                    f"\\shad{_fmt_num(outer_shadow)}\\4c&H000000&"
                    f"\\blur{_fmt_num(outer_blur)}}}{chs_plain}"
                )
                dialogues.append(
                    f"Dialogue: 1,{start_time},{end_time},CHS,,0,0,0,,{out_text}"
                )

            chs_alignments = word_alignments if source_language.startswith("zh") else None
            if use_karaoke and karaoke_mode in {"k", "kf"}:
                chs_text = format_multiline_karaoke(
                    layout.chs_lines,
                    duration_sec,
                    word_alignments=chs_alignments,
                    is_cjk=True,
                    mode=karaoke_mode,
                )
            else:
                chs_text = "\\N".join(pos.text for pos in layout.chs_lines)
            dialogue_text = f"{pos_prefix}\\fs{pos0.font_size}{soft_entry_tags}{base_blur_tag}}}{chs_text}"
            dialogues.append(
                f"Dialogue: 1,{start_time},{end_time},CHS,,0,0,0,,{dialogue_text}"
            )
            if use_karaoke and karaoke_mode == "clip" and len(layout.chs_lines) == 1:
                clip_tags = generate_clip_karaoke_overlay(
                    layout.chs_lines[0],
                    duration_sec,
                    chs_alignments,
                    font_path=chs_font_path,
                )
                if clip_tags:
                    clip_text = getattr(layout.chs_lines[0], "text", str(layout.chs_lines[0]))
                    overlay = (
                        f"{pos_prefix}\\fs{pos0.font_size}\\1c&H00E8FF&"
                        f"\\bord0\\shad0{clip_tags}}}{clip_text}"
                    )
                    dialogues.append(
                        f"Dialogue: 4,{start_time},{end_time},CHS,,0,0,0,,{overlay}"
                    )

    if overflow_report_path is not None:
        _atomic_write(
            overflow_report_path,
            json.dumps(overflow_records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if overflow_records:
        report_hint = str(overflow_report_path) if overflow_report_path is not None else "overflow report"
        raise ValueError(
            f"ASS rendering blocked: {len(overflow_records)} subtitle entries failed layout. "
            f"No partial ASS was produced. See {report_hint} for details."
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
    overflow_report_path: Path | None = None,
    config: SubtitleRenderConfig | None = None,
    enable_karaoke: bool | None = None,
    enable_frosted_glass: bool | None = None,
    enable_multi_layer_outline: bool | None = None,
    enable_kinetic: bool | None = None,
    kinetic_options: dict[str, Any] | None = None,
) -> None:
    report_path = overflow_report_path or (path.parent / "ass_layout_overflow_report.json")
    try:
        rendered = render_ass(
            entries,
            source_language=source_language,
            target_language=target_language,
            overflow_report_path=report_path,
            config=config,
            enable_karaoke=enable_karaoke,
            enable_frosted_glass=enable_frosted_glass,
            enable_multi_layer_outline=enable_multi_layer_outline,
            enable_kinetic=enable_kinetic,
            kinetic_options=kinetic_options,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    _atomic_write(path, rendered, encoding="utf-8-sig")
