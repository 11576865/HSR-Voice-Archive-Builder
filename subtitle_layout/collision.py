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


def check_bilingual_collision(
    chs_block: LayoutBlock,
    primary_block: LayoutBlock,
    safe_area: SafeArea = DEFAULT_SAFE_AREA,
    min_central_gap: float = 40.0,
) -> bool:
    """Return True if there is a collision or safe area violation, False if valid."""
    # Check left/right overflow for Chinese lines
    for line in chs_block.lines:
        if measure_text_width(line, chs_block.font_size) > safe_area.max_printable_width:
            return True

    # Check left/right overflow for Primary lines
    for line in primary_block.lines:
        if measure_text_width(line, primary_block.font_size) > safe_area.max_printable_width:
            return True

    # Check vertical bounds for Chinese block (anchored top, grows downward)
    if chs_block.lines and chs_block.y_start < safe_area.y_min:
        return True

    # Check vertical bounds for Primary block (anchored bottom, grows upward)
    if primary_block.lines and primary_block.y_end > safe_area.y_max:
        return True

    # Check central safety gap between Chinese block bottom and Primary block top
    if chs_block.lines and primary_block.lines:
        if chs_block.y_end + min_central_gap > primary_block.y_start:
            return True

    return False
