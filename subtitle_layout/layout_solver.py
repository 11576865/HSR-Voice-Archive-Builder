from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

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
) -> SolvedLayout:
    same_chinese = source_language == target_language == "zh-CN"
    center_x = safe_area.canvas_width // 2

    # Central boundary coordinates
    y_center = safe_area.canvas_height / 2.0
    y_center_top = round(y_center - min_central_gap / 2.0)
    y_center_bottom = round(y_center + min_central_gap / 2.0)

    scale_attempts: list[int] = []
    last_failure_condition: str | None = None

    for scale in SCALE_FACTORS:
        scale_percent = round(scale * 100)
        scale_attempts.append(scale_percent)

        chs_size = get_scaled_font_size(base_chs_size, scale)
        primary_size = get_scaled_font_size(base_primary_size, scale)

        if same_chinese or not english_text.strip():
            pri_broken = []
            chs_input = chinese_text or english_text
            chs_broken = break_line(chs_input, safe_area.max_printable_width, chs_size)
        elif not chinese_text.strip():
            pri_broken = break_line(english_text, safe_area.max_printable_width, primary_size)
            chs_broken = []
        else:
            pri_broken = break_line(english_text, safe_area.max_printable_width, primary_size)
            chs_broken = break_line(chinese_text, safe_area.max_printable_width, chs_size)

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
        )
        pri_block = LayoutBlock(
            lines=pri_broken,
            font_size=primary_size,
            y_start=pri_start,
            y_end=pri_end,
            max_width=safe_area.max_printable_width,
        )

        has_collision, failure_condition = check_bilingual_collision_with_reason(
            chs_block, pri_block, safe_area=safe_area, min_central_gap=min_central_gap
        )

        if not has_collision:
            pri_positions: list[SubtitleLinePos] = []
            for line in pri_broken:
                pri_positions.append(
                    SubtitleLinePos(
                        text=line,
                        font_size=primary_size,
                        x=center_x,
                        y=y_center_top,
                        alignment=2,  # Bottom-Center alignment for Primary
                    )
                )

            chs_positions: list[SubtitleLinePos] = []
            for line in chs_broken:
                chs_positions.append(
                    SubtitleLinePos(
                        text=line,
                        font_size=chs_size,
                        x=center_x,
                        y=y_center_bottom,
                        alignment=8,  # Top-Center alignment for Secondary
                    )
                )

            return SolvedLayout(
                scale_factor=scale,
                chs_lines=chs_positions,
                primary_lines=pri_positions,
                failed=False,
                failed_condition=None,
                scale_attempts=scale_attempts,
            )

        last_failure_condition = failure_condition

    # Rejection if all scaling attempts fail
    return SolvedLayout(
        scale_factor=SCALE_FACTORS[-1],
        chs_lines=[],
        primary_lines=[],
        failed=True,
        failed_condition=last_failure_condition or "OVERFLOW",
        scale_attempts=scale_attempts,
    )
