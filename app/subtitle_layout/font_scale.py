from __future__ import annotations

from collections.abc import Callable


# Based on initial size, not multiplicative scaling.
DEFAULT_SCALE_STEPS = (100, 95, 90, 85)


def resolve_font_size(
    initial_size: int,
    fits: Callable[[int], bool],
    steps: tuple[int, ...] = DEFAULT_SCALE_STEPS,
) -> tuple[int, int]:
    """Return (font_size, applied_scale_percent)."""
    for percent in steps:
        size = round(initial_size * percent / 100)
        if fits(size):
            return size, percent

    return round(initial_size * steps[-1] / 100), steps[-1]
