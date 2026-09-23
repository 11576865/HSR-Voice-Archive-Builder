from __future__ import annotations

from typing import Any
from .safe_area import SafeArea
from .font_scale import (
    DEFAULT_BASE_FONT_SIZE_CHS,
    DEFAULT_BASE_FONT_SIZE_PRIMARY,
    SCALE_FACTORS,
    get_scaled_font_size,
)
from .measure import measure_line_height, measure_text_width
from .collision import LayoutBlock, check_bilingual_collision_with_reason
from .breaker import break_line
from .dynamic_scaler import calculate_target_font_size


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
    margin_left_percent = max(0.0, min(0.40, float(margin_left_percent)))
    margin_top_percent = max(0.0, min(0.40, float(margin_top_percent)))
    safe_area = SafeArea(
        canvas_width=1920,
        canvas_height=1080,
        margin_left_percent=margin_left_percent,
        margin_right_percent=margin_left_percent,
        margin_top_percent=margin_top_percent,
        margin_bottom_percent=margin_top_percent,
    )

    min_central_gap = max(0.0, min(200.0, float(min_central_gap)))
    same_chinese = source_language == target_language == "zh-CN"
    center_x = safe_area.canvas_width // 2

    y_center = safe_area.canvas_height / 2.0
    y_center_top = round(y_center - min_central_gap / 2.0)
    y_center_bottom = round(y_center + min_central_gap / 2.0)

    # 1. Chunking
    chs_input = chinese_text or english_text
    if same_chinese or not english_text.strip():
        pre_pri: list[str] = []
        pre_chs = break_line(chs_input, safe_area.max_printable_width, base_chs_size)
    elif not chinese_text.strip():
        pre_pri = break_line(english_text, safe_area.max_printable_width, base_primary_size)
        pre_chs = []
    else:
        pre_pri = break_line(english_text, safe_area.max_printable_width, base_primary_size)
        pre_chs = break_line(chinese_text, safe_area.max_printable_width, base_chs_size)

    # 2. Dynamic base sizing
    if pre_chs:
        chunked_chs_text = r"\N".join(pre_chs)
        calc_chs, _ = calculate_target_font_size(chunked_chs_text, base_font_size=base_chs_size, max_width=safe_area.max_printable_width)
        effective_base_chs = min(max(calc_chs, 38), 64)
    else:
        effective_base_chs = base_chs_size

    if pre_pri:
        chunked_pri_text = r"\N".join(pre_pri)
        calc_pri, _ = calculate_target_font_size(chunked_pri_text, base_font_size=base_primary_size, max_width=safe_area.max_printable_width)
        effective_base_pri = min(max(calc_pri, 32), 54)
    else:
        effective_base_pri = base_primary_size

    scale_attempts: list[int] = []
    chosen_scale = SCALE_FACTORS[-1]
    chosen_pri_broken: list[str] = []
    chosen_chs_broken: list[str] = []
    chosen_pri_size = base_primary_size
    chosen_chs_size = base_chs_size
    has_collision = True
    failure_condition: str | None = None

    for scale in SCALE_FACTORS:
        scale_percent = round(scale * 100)
        scale_attempts.append(scale_percent)

        chs_size = get_scaled_font_size(effective_base_chs, scale)
        primary_size = get_scaled_font_size(effective_base_pri, scale)

        if same_chinese or not english_text.strip():
            pri_broken: list[str] = []
            chs_broken = break_line(chs_input, safe_area.max_printable_width, chs_size)
        elif not chinese_text.strip():
            pri_broken = break_line(english_text, safe_area.max_printable_width, primary_size)
            chs_broken = []
        else:
            pri_broken = break_line(english_text, safe_area.max_printable_width, primary_size)
            chs_broken = break_line(chinese_text, safe_area.max_printable_width, chs_size)

        chs_lh = measure_line_height(chs_size)
        pri_lh = measure_line_height(primary_size)

        pri_height = len(pri_broken) * pri_lh if pri_broken else 0.0
        pri_end = y_center_top
        pri_start = pri_end - pri_height

        chs_height = len(chs_broken) * chs_lh if chs_broken else 0.0
        chs_start = y_center_bottom
        chs_end = chs_start + chs_height

        chs_block = LayoutBlock(
            lines=chs_broken,
            font_size=chs_size,
            y_start=chs_start,
            y_end=chs_end,
            max_width=safe_area.max_printable_width,
        )
        pri_block = LayoutBlock(
            lines=pri_broken,
            font_size=primary_size,
            y_start=pri_start,
            y_end=pri_end,
            max_width=safe_area.max_printable_width,
        )

        collision, reason = check_bilingual_collision_with_reason(
            chs_block, pri_block, safe_area=safe_area, min_central_gap=min_central_gap
        )

        chosen_scale = scale
        chosen_pri_broken = pri_broken
        chosen_chs_broken = chs_broken
        chosen_pri_size = primary_size
        chosen_chs_size = chs_size
        has_collision = collision
        failure_condition = reason

        if not collision:
            break

    pri_lh = measure_line_height(chosen_pri_size)
    pri_lines_info = []
    pri_height = len(chosen_pri_broken) * pri_lh
    pri_start = y_center_top - pri_height
    for i, line_text in enumerate(chosen_pri_broken):
        w = measure_text_width(line_text, chosen_pri_size)
        line_top = pri_start + i * pri_lh
        pri_lines_info.append({
            "text": line_text,
            "font_size": chosen_pri_size,
            "measured_width": w,
            "line_height": pri_lh,
            "x": center_x,
            "y": y_center_top,
            "alignment": 2,
            "bbox": {
                "x_min": center_x - w / 2.0,
                "x_max": center_x + w / 2.0,
                "y_min": line_top,
                "y_max": line_top + pri_lh,
            }
        })

    chs_lh = measure_line_height(chosen_chs_size)
    chs_lines_info = []
    chs_start = y_center_bottom
    for i, line_text in enumerate(chosen_chs_broken):
        w = measure_text_width(line_text, chosen_chs_size)
        line_top = chs_start + i * chs_lh
        chs_lines_info.append({
            "text": line_text,
            "font_size": chosen_chs_size,
            "measured_width": w,
            "line_height": chs_lh,
            "x": center_x,
            "y": y_center_bottom,
            "alignment": 8,
            "bbox": {
                "x_min": center_x - w / 2.0,
                "x_max": center_x + w / 2.0,
                "y_min": line_top,
                "y_max": line_top + chs_lh,
            }
        })

    pri_block_info = {
        "y_start": pri_start,
        "y_end": y_center_top,
        "height": pri_height,
        "line_count": len(chosen_pri_broken),
        "font_size": chosen_pri_size,
    }
    chs_height = len(chosen_chs_broken) * chs_lh
    chs_block_info = {
        "y_start": y_center_bottom,
        "y_end": y_center_bottom + chs_height,
        "height": chs_height,
        "line_count": len(chosen_chs_broken),
        "font_size": chosen_chs_size,
    }

    # Parallax region calculation: center-outward expansion displacement
    primary_offset = pri_height
    chs_offset = chs_height
    total_span = pri_height + min_central_gap + chs_height
    parallax_ratio = round(pri_height / chs_height, 3) if chs_height > 0 else (1.0 if pri_height == 0 else 999.0)

    parallax_info = {
        "primary_offset": primary_offset,
        "chs_offset": chs_offset,
        "total_span": total_span,
        "parallax_ratio": parallax_ratio,
        "y_primary_peak": pri_start,
        "y_chs_peak": y_center_bottom + chs_height,
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
            "scale_factor": chosen_scale,
            "scale_percent": round(chosen_scale * 100),
            "scale_attempts": scale_attempts,
            "failed": has_collision,
            "failed_condition": failure_condition if has_collision else None,
            "effective_base_chs": effective_base_chs,
            "effective_base_pri": effective_base_pri,
            "primary_lines": pri_lines_info,
            "chs_lines": chs_lines_info,
            "primary_block": pri_block_info,
            "chs_block": chs_block_info,
        },
    }
