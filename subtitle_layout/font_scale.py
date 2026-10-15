from __future__ import annotations

from typing import Sequence

# Exhaust the bounded readability budget instead of stopping at 85%.
# A failed 85% plan is NOT proof that the text cannot fit at a smaller size.
# The 25% floor keeps a 48px base caption at 12px; below this require an
# explicit authoring change rather than silently producing illegible output.
SCALE_FACTORS: Sequence[float] = tuple(round(i / 100, 2) for i in range(100, 24, -5))

DEFAULT_BASE_FONT_SIZE_CHS: int = 52
DEFAULT_BASE_FONT_SIZE_PRIMARY: int = 42


def get_scaled_font_size(base_font_size: int, scale_factor: float) -> int:
    """Always calculate scaled size directly from original base font size."""
    return max(1, round(base_font_size * scale_factor))
