from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .breaker import break_line
from .collision import LayoutBlock, check_bilingual_collision
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
    alignment: int  # ASS alignment tag value, e.g. 8 for top-center


@dataclass
class SolvedLayout:
    scale_factor: float
    chs_lines: list[SubtitleLinePos]
    primary_lines: list[SubtitleLinePos]


def solve_subtitle_layout(
    english_text: str,
    chinese_text: str,
    source_language: str = "en",
    target_language: str = "zh-CN",
    base_chs_size: int = DEFAULT_BASE_FONT_SIZE_CHS,
    base_primary_size: int = DEFAULT_BASE_FONT_SIZE_PRIMARY,
    safe_area: SafeArea = DEFAULT_SAFE_AREA,
    min_central_gap: float = 40.0,
) -> SolvedLayout:
    same_chinese = source_language == target_language == "zh-CN"
    center_x = safe_area.canvas_width // 2

    for scale in SCALE_FACTORS:
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
            chs_broken = break_line(chinese_text, safe_area.max_printable_width, chs_size)
            pri_broken = break_line(english_text, safe_area.max_printable_width, primary_size)

        chs_lh = measure_line_height(chs_size)
        pri_lh = measure_line_height(primary_size)

        # Chinese block anchored at top safe boundary (y_min), grows downward
        chs_top = safe_area.y_min
        chs_bottom = chs_top + (len(chs_broken) * chs_lh if chs_broken else 0)

        # Primary block anchored at bottom safe boundary (y_max), grows upward
        pri_bottom = safe_area.y_max
        pri_top = pri_bottom - (len(pri_broken) * pri_lh if pri_broken else 0)

        chs_block = LayoutBlock(
            lines=chs_broken,
            font_size=chs_size,
            y_start=chs_top,
            y_end=chs_bottom,
            max_width=safe_area.max_printable_width,
        )
        pri_block = LayoutBlock(
            lines=pri_broken,
            font_size=primary_size,
            y_start=pri_top,
            y_end=pri_bottom,
            max_width=safe_area.max_printable_width,
        )

        has_collision = check_bilingual_collision(
            chs_block, pri_block, safe_area=safe_area, min_central_gap=min_central_gap
        )

        if not has_collision or scale == SCALE_FACTORS[-1]:
            chs_positions: list[SubtitleLinePos] = []
            for i, line in enumerate(chs_broken):
                line_y = round(chs_top + i * chs_lh)
                chs_positions.append(
                    SubtitleLinePos(
                        text=line,
                        font_size=chs_size,
                        x=center_x,
                        y=line_y,
                        alignment=8,
                    )
                )

            pri_positions: list[SubtitleLinePos] = []
            for i, line in enumerate(pri_broken):
                line_y = round(pri_top + i * pri_lh)
                pri_positions.append(
                    SubtitleLinePos(
                        text=line,
                        font_size=primary_size,
                        x=center_x,
                        y=line_y,
                        alignment=8,
                    )
                )

            return SolvedLayout(
                scale_factor=scale,
                chs_lines=chs_positions,
                primary_lines=pri_positions,
            )

    return SolvedLayout(
        scale_factor=SCALE_FACTORS[-1],
        chs_lines=[],
        primary_lines=[],
    )
