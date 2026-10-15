from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from .breaker import break_line
from .collision import LayoutBlock, check_bilingual_collision_with_reason
from .font_scale import (
    DEFAULT_BASE_FONT_SIZE_CHS,
    DEFAULT_BASE_FONT_SIZE_PRIMARY,
    SCALE_FACTORS,
    get_scaled_font_size,
)
from .measure import measure_line_height
from .safe_area import DEFAULT_SAFE_AREA, SafeArea


@dataclass
class SubtitleLinePos:
    text: str
    font_size: int
    x: int
    y: int
    alignment: int  # ASS alignment tag value: 2 for bottom-center (Primary), 8 for top-center (Secondary)


@dataclass
class SolvedLayout:
    scale_factor: float
    chs_lines: list[SubtitleLinePos]
    primary_lines: list[SubtitleLinePos]
    failed: bool = False
    failed_condition: str | None = None
    scale_attempts: list[int] = field(default_factory=list)


def solve_subtitle_layout(
    english_text: str,
    chinese_text: str,
    source_language: str = "en",
    target_language: str = "zh-CN",
    base_chs_size: int = DEFAULT_BASE_FONT_SIZE_CHS,
    base_primary_size: int = DEFAULT_BASE_FONT_SIZE_PRIMARY,
    safe_area: SafeArea = DEFAULT_SAFE_AREA,
    min_central_gap: float = 20.0,
    primary_region_share: float = 0.60,
    chs_font_path: str | None = None,
    primary_font_path: str | None = None,
    primary_width_factor: float = 1.0,
    chs_width_factor: float = 1.0,
    central_ink_padding: float = 0.0,
    ink_validator: Callable[[SolvedLayout], tuple[bool, str | None]] | None = None,
) -> SolvedLayout:
    same_chinese = source_language == target_language == "zh-CN"
    center_x = safe_area.canvas_width // 2

    # Central boundary coordinates.
    # After reserving the central gap, allocate the remaining safe-area height
    # 60% to the primary/source subtitle region above the gap and 40% to the
    # Chinese target subtitle region below it.
    # ASS positioning anchors are NOT the visible glyph edges. Reserve extra
    # room for physical stroke/shadow spill; keep the requested central gap
    # as the minimum separation between actual rendered ink.
    bilingual = bool(english_text.strip() and chinese_text.strip()) and not same_chinese
    anchor_gap = max(0.0, float(min_central_gap)) + (
        max(0.0, float(central_ink_padding)) if bilingual else 0.0
    )
    available_height = max(0.0, safe_area.max_printable_height - anchor_gap)
    primary_region_height = available_height * max(0.20, min(0.80, float(primary_region_share)))
    y_center_top = round(safe_area.y_min + primary_region_height)
    y_center_bottom = round(y_center_top + anchor_gap)

    scale_attempts: list[int] = []
    last_failure_condition: str | None = None
    last_pri_positions: list[SubtitleLinePos] = []
    last_chs_positions: list[SubtitleLinePos] = []

    # 1. Preliminary semantic line-breaking at base font sizes
    chs_input = chinese_text or english_text
    if same_chinese or not english_text.strip():
        pre_pri = []
        pre_chs = break_line(chs_input, safe_area.max_printable_width, base_chs_size, chs_font_path)
    elif not chinese_text.strip():
        pre_pri = break_line(english_text, safe_area.max_printable_width, base_primary_size, primary_font_path)
        pre_chs = []
    else:
        pre_pri = break_line(english_text, safe_area.max_printable_width, base_primary_size, primary_font_path)
        pre_chs = break_line(chinese_text, safe_area.max_printable_width, base_chs_size, chs_font_path)

    # 2. Base font sizes are strictly consistent to preserve stable visual hierarchy
    effective_base_chs = base_chs_size
    effective_base_pri = base_primary_size

    # Width correction is supplied by actual libass pixel evidence, never a
    # hard-coded font-specific UI multiplier. Keep a bounded search budget.
    primary_width_limit = safe_area.max_printable_width / max(0.65, min(1.55, primary_width_factor))
    chs_width_limit = safe_area.max_printable_width / max(0.65, min(1.55, chs_width_factor))

    for scale in SCALE_FACTORS:
        scale_percent = round(scale * 100)
        scale_attempts.append(scale_percent)

        chs_size = get_scaled_font_size(effective_base_chs, scale)
        primary_size = get_scaled_font_size(effective_base_pri, scale)

        if same_chinese or not english_text.strip():
            pri_broken = []
            chs_input = chinese_text or english_text
            chs_broken = break_line(chs_input, chs_width_limit, chs_size, chs_font_path)
        elif not chinese_text.strip():
            pri_broken = break_line(english_text, primary_width_limit, primary_size, primary_font_path)
            chs_broken = []
        else:
            pri_broken = break_line(english_text, primary_width_limit, primary_size, primary_font_path)
            chs_broken = break_line(chinese_text, chs_width_limit, chs_size, chs_font_path)

        chs_lh = measure_line_height(chs_size)
        pri_lh = measure_line_height(primary_size)

        # Primary Language Block (Top Side): anchored at y_center_top, grows upward towards y_min
        pri_height = len(pri_broken) * pri_lh if pri_broken else 0.0
        pri_end = y_center_top
        pri_start = pri_end - pri_height

        # Secondary Language Block (Bottom Side / CHS): anchored at y_center_bottom, grows downward towards y_max
        chs_height = len(chs_broken) * chs_lh if chs_broken else 0.0
        chs_start = y_center_bottom
        chs_end = chs_start + chs_height

        chs_block = LayoutBlock(
            lines=chs_broken,
            font_size=chs_size,
            y_start=chs_start,
            y_end=chs_end,
            max_width=safe_area.max_printable_width,
            font_path=chs_font_path,
        )
        pri_block = LayoutBlock(
            lines=pri_broken,
            font_size=primary_size,
            y_start=pri_start,
            y_end=pri_end,
            max_width=safe_area.max_printable_width,
            font_path=primary_font_path,
        )

        has_collision, failure_condition = check_bilingual_collision_with_reason(
            chs_block, pri_block, safe_area=safe_area, min_central_gap=min_central_gap
        )

        pri_positions: list[SubtitleLinePos] = [
            SubtitleLinePos(
                text=line,
                font_size=primary_size,
                x=center_x,
                y=y_center_top,
                alignment=2,
            )
            for line in pri_broken
        ]
        chs_positions: list[SubtitleLinePos] = [
            SubtitleLinePos(
                text=line,
                font_size=chs_size,
                x=center_x,
                y=y_center_bottom,
                alignment=8,
            )
            for line in chs_broken
        ]
        last_pri_positions = pri_positions
        last_chs_positions = chs_positions

        candidate = SolvedLayout(
            scale_factor=scale, chs_lines=chs_positions,
            primary_lines=pri_positions, scale_attempts=list(scale_attempts),
        )
        if ink_validator is not None:
            # The actual rasterization is authoritative. This correctly
            # accepts a candidate when PIL overestimates width, and rejects
            # one when true outlines extend beyond its synthetic rectangles.
            valid, real_reason = ink_validator(candidate)
            if valid:
                return candidate
            last_failure_condition = real_reason or "LIBASS_INK_REJECTED"
            continue
        if not has_collision:
            return candidate

        last_failure_condition = failure_condition

    # Rejection if all scaling attempts fail
    return SolvedLayout(
        scale_factor=SCALE_FACTORS[-1],
        chs_lines=last_chs_positions,
        primary_lines=last_pri_positions,
        failed=True,
        failed_condition=last_failure_condition or "OVERFLOW",
        scale_attempts=scale_attempts,
    )
