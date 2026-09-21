from __future__ import annotations

import unicodedata



def glyph_width_factor(char: str) -> float:
    """Cheap layout estimation without requiring font rendering libraries."""
    if char.isspace():
        return 0.35
    if unicodedata.east_asian_width(char) in {"W", "F"}:
        return 1.0
    if char in "ilI.,'!|:;":
        return 0.3
    if char in "MW@#%&":
        return 0.9
    return 0.55



def estimate_width(text: str, font_size: int) -> int:
    return int(sum(glyph_width_factor(c) for c in text) * font_size)



def estimate_height(line_count: int, font_size: int, line_spacing: float = 1.25) -> int:
    return int(line_count * font_size * line_spacing)



def fits(text: str, font_size: int, max_width: int) -> bool:
    return estimate_width(text, font_size) <= max_width
