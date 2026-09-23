from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .measure import measure_line_height, measure_text_width
from .safe_area import DEFAULT_SAFE_AREA, SafeArea


@dataclass
class LayoutBlock:
    lines: Sequence[str]
    font_size: int
    y_start: float
    y_end: float
    max_width: float


def check_bilingual_collision_with_reason(
    chs_block: LayoutBlock,
    primary_block: LayoutBlock,
    safe_area: SafeArea = DEFAULT_SAFE_AREA,
    min_central_gap: float = 20.0,
) -> tuple[bool, str | None]:
    """Safety and boundary validator for Center-Outward Bilingual Subtitles.

    Primary Language Block (Top Side):
      - Growing upward from Y_center_top towards top margin (y_min = 54px).
      - Top boundary: Top_Primary = primary_block.y_start
      - Bottom boundary: Y_center_top = primary_block.y_end

    Secondary Language Block (Bottom Side / CHS):
      - Growing downward from Y_center_bottom towards bottom margin (y_max = 1026px).
      - Top boundary: Y_center_bottom = chs_block.y_start
      - Bottom boundary: Bottom_Secondary = chs_block.y_end

    Collision Check List:
    1. Horizontal overflow for any line > safe_area.max_printable_width
    2. Primary top boundary < safe_area.y_min (TOP_OVERFLOW)
    3. Secondary bottom boundary > safe_area.y_max (BOTTOM_OVERFLOW)
    4. Inter-block central gap < min_central_gap (CENTRAL_COLLISION)

    Returns:
        (has_collision: bool, failure_condition: str | None)
    """
    # 1. Primary horizontal overflow
    for line in primary_block.lines:
        if measure_text_width(line, primary_block.font_size) > safe_area.max_printable_width:
            return True, "HORIZONTAL_OVERFLOW"

    # 2. Chinese / Secondary horizontal overflow
    for line in chs_block.lines:
        if measure_text_width(line, chs_block.font_size) > safe_area.max_printable_width:
            return True, "HORIZONTAL_OVERFLOW"

    # 3. Primary vertical bounds check (growing upward towards y_min)
    if primary_block.lines:
        if primary_block.y_start < safe_area.y_min:
            return True, "TOP_OVERFLOW"
        if primary_block.y_end > safe_area.y_max:
            return True, "BOTTOM_OVERFLOW"

    # 4. Chinese / Secondary vertical bounds check (growing downward towards y_max)
    if chs_block.lines:
        if chs_block.y_start < safe_area.y_min:
            return True, "TOP_OVERFLOW"
        if chs_block.y_end > safe_area.y_max:
            return True, "BOTTOM_OVERFLOW"

    # 5. Central gap check between Primary and Secondary
    if primary_block.lines and chs_block.lines:
        if primary_block.y_end + min_central_gap > chs_block.y_start:
            return True, "CENTRAL_COLLISION"

    return False, None


def check_bilingual_collision(
    chs_block: LayoutBlock,
    primary_block: LayoutBlock,
    safe_area: SafeArea = DEFAULT_SAFE_AREA,
    min_central_gap: float = 20.0,
) -> bool:
    """Return True if there is a collision or safe area violation, False if valid."""
    has_collision, _ = check_bilingual_collision_with_reason(
        chs_block, primary_block, safe_area=safe_area, min_central_gap=min_central_gap
    )
    return has_collision
