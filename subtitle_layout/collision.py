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
    """Symmetric 4-bound safety check for both Chinese and Primary blocks.

    Canvas Bounds (1920x1080):
    - Horizontal bounds: 192px <= x <= 1728px (max printable width: 1536px)
    - Vertical bounds: 54px <= y <= 1026px

    Collision Check List:
    1. Chinese block top < 54px OR bottom > 1026px
    2. Primary block top < 54px OR bottom > 1026px
    3. Left/Right overflow for any line > 1536px
    4. Inter-block central distance < Minimum Safety Gap (20px)

    Returns:
        (has_collision: bool, failure_condition: str | None)
    """
    # 1. Chinese horizontal overflow
    for line in chs_block.lines:
        if measure_text_width(line, chs_block.font_size) > safe_area.max_printable_width:
            return True, "HORIZONTAL_OVERFLOW"

    # 2. Primary horizontal overflow
    for line in primary_block.lines:
        if measure_text_width(line, primary_block.font_size) > safe_area.max_printable_width:
            return True, "HORIZONTAL_OVERFLOW"

    # 3. Chinese vertical bounds (symmetric top & bottom checks)
    if chs_block.lines:
        if chs_block.y_start < safe_area.y_min:
            return True, "TOP_OVERFLOW"
        if chs_block.y_end > safe_area.y_max:
            return True, "BOTTOM_OVERFLOW"

    # 4. Primary vertical bounds (symmetric top & bottom checks)
    if primary_block.lines:
        if primary_block.y_start < safe_area.y_min:
            return True, "TOP_OVERFLOW"
        if primary_block.y_end > safe_area.y_max:
            return True, "BOTTOM_OVERFLOW"

    # 5. Central gap check
    if chs_block.lines and primary_block.lines:
        if chs_block.y_end + min_central_gap > primary_block.y_start:
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
