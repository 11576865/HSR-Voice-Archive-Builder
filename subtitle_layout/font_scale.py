from __future__ import annotations

from typing import Sequence

# Font scaling factors in order of attempt: 100% -> 95% -> 90% -> 85%
SCALE_FACTORS: Sequence[float] = (1.0, 0.95, 0.90, 0.85)

DEFAULT_BASE_FONT_SIZE_CHS: int = 52
DEFAULT_BASE_FONT_SIZE_PRIMARY: int = 42


def get_scaled_font_size(base_font_size: int, scale_factor: float) -> int:
    """Always calculate scaled size directly from original base font size."""
    return max(1, round(base_font_size * scale_factor))
