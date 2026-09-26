from __future__ import annotations

from typing import Any

from .font_scale import (
    DEFAULT_BASE_FONT_SIZE_CHS,
    DEFAULT_BASE_FONT_SIZE_PRIMARY,
)
from .layout_solver import SolvedLayout, SubtitleLinePos, solve_subtitle_layout
from .measure import measure_line_height, measure_text_width
from .safe_area import SafeArea


def _line_info(
    lines: list[SubtitleLinePos],
    *,
    is_primary: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not lines:
        return [], {
            "y_start": 0.0,
            "y_end": 0.0,
            "height": 0.0,
            "line_count": 0,
            "font_size": 0,
        }

    font_size = lines[0].font_size
    line_height = measure_line_height(font_size)
    anchor_y = float(lines[0].y)
    block_height = len(lines) * line_height
    block_start = anchor_y - block_height if is_primary else anchor_y
    block_end = anchor_y if is_primary else anchor_y + block_height

    info: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        width = measure_text_width(line.text, line.font_size)
        line_top = block_start + index * line_height
        info.append(
            {
                "text": line.text,
                "font_size": line.font_size,
                "measured_width": width,
                "line_height": line_height,
                "x": line.x,
                "y": line.y,
                "alignment": line.alignment,
                "bbox": {
                    "x_min": line.x - width / 2.0,
                    "x_max": line.x + width / 2.0,
                    "y_min": line_top,
                    "y_max": line_top + line_height,
                },
            }
        )

    return info, {
        "y_start": block_start,
        "y_end": block_end,
        "height": block_height,
        "line_count": len(lines),
        "font_size": font_size,
    }


def preview_subtitle_layout(
    english_text: str = "May this journey lead us starward.",
    chinese_text: str = "愿此行，终抵群星。",
    source_language: str = "en",
    target_language: str = "zh-CN",
    base_chs_size: int = DEFAULT_BASE_FONT_SIZE_CHS,
    base_primary_size: int = DEFAULT_BASE_FONT_SIZE_PRIMARY,
    margin_left_percent: float = 0.10,
    margin_top_percent: float = 0.05,
    min_central_gap: float = 20.0,
) -> dict[str, Any]:
    """Return browser-preview geometry from the exact ASS layout solver."""

    margin_left_percent = max(0.0, min(0.40, float(margin_left_percent)))
    margin_top_percent = max(0.0, min(0.40, float(margin_top_percent)))
    min_central_gap = max(0.0, min(200.0, float(min_central_gap)))

    safe_area = SafeArea(
        canvas_width=1920,
        canvas_height=1080,
        margin_left_percent=margin_left_percent,
        margin_right_percent=margin_left_percent,
        margin_top_percent=margin_top_percent,
        margin_bottom_percent=margin_top_percent,
    )

    solved: SolvedLayout = solve_subtitle_layout(
        english_text=english_text,
        chinese_text=chinese_text,
        source_language=source_language,
        target_language=target_language,
        base_chs_size=int(base_chs_size),
        base_primary_size=int(base_primary_size),
        safe_area=safe_area,
        min_central_gap=min_central_gap,
    )

    primary_lines, primary_block = _line_info(
        solved.primary_lines,
        is_primary=True,
    )
    chs_lines, chs_block = _line_info(
        solved.chs_lines,
        is_primary=False,
    )

    y_center = safe_area.canvas_height / 2.0
    y_center_top = round(y_center - min_central_gap / 2.0)
    y_center_bottom = round(y_center + min_central_gap / 2.0)

    primary_height = float(primary_block["height"])
    chs_height = float(chs_block["height"])
    parallax_info = {
        "primary_offset": primary_height,
        "chs_offset": chs_height,
        "total_span": primary_height + min_central_gap + chs_height,
        "parallax_ratio": (
            round(primary_height / chs_height, 3)
            if chs_height > 0
            else (1.0 if primary_height == 0 else 999.0)
        ),
        "y_primary_peak": primary_block["y_start"],
        "y_chs_peak": chs_block["y_end"],
    }

    return {
        "ok": True,
        "canvas": {
            "width": safe_area.canvas_width,
            "height": safe_area.canvas_height,
        },
        "safe_area": {
            "margin_left": safe_area.margin_left,
            "margin_right": safe_area.margin_right,
            "margin_top": safe_area.margin_top,
            "margin_bottom": safe_area.margin_bottom,
            "x_min": safe_area.x_min,
            "x_max": safe_area.x_max,
            "y_min": safe_area.y_min,
            "y_max": safe_area.y_max,
            "max_printable_width": safe_area.max_printable_width,
            "max_printable_height": safe_area.max_printable_height,
            "margin_left_percent": safe_area.margin_left_percent,
            "margin_top_percent": safe_area.margin_top_percent,
        },
        "central_gap": {
            "y_center": y_center,
            "y_top": y_center_top,
            "y_bottom": y_center_bottom,
            "min_central_gap": min_central_gap,
        },
        "parallax": parallax_info,
        "layout": {
            "scale_factor": solved.scale_factor,
            "scale_percent": round(solved.scale_factor * 100),
            "scale_attempts": solved.scale_attempts,
            "failed": solved.failed,
            "failed_condition": solved.failed_condition,
            "effective_base_chs": int(base_chs_size),
            "effective_base_pri": int(base_primary_size),
            "primary_lines": primary_lines,
            "chs_lines": chs_lines,
            "primary_block": primary_block,
            "chs_block": chs_block,
        },
    }
