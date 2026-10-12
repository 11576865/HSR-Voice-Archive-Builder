"""Pixel evidence from TWO identically timestamped libass renders.

Black-and-white backgrounds prevent invisible dark shadow pixels or white glyph
pixels from disappearing into a single reference. Pixel threshold is deliberately
a declared detection cutoff, not an exact mathematical alpha-boundary proof.
"""
from __future__ import annotations


def visible_ink_bbox(
    black_rgb: bytes, white_rgb: bytes, width: int, height: int,
    *, threshold: int = 16,
) -> tuple[int, int, int, int] | None:
    """Half-open XY bbox of visible effects, or None when below threshold.

    RGB24 frames must come from the same exact ASS render timestamp on
    neutral black and white backgrounds, with no lossy video encoding.
    """
    if width <= 0 or height <= 0 or not 1 <= threshold <= 255:
        raise ValueError("Invalid rendered-ink dimensions or cutoff")
    expected = width * height * 3
    if len(black_rgb) != expected or len(white_rgb) != expected:
        raise ValueError("Rendered-ink RGB frame dimensions mismatch")
    black, white = memoryview(black_rgb), memoryview(white_rgb)
    x_min, y_min, x_max, y_max = width, height, -1, -1
    limit = 255 - threshold
    for pixel in range(width * height):
        at = pixel * 3
        if (
            black[at] < threshold and black[at + 1] < threshold and
            black[at + 2] < threshold and white[at] > limit and
            white[at + 1] > limit and white[at + 2] > limit
        ):
            continue
        x, y = pixel % width, pixel // width
        x_min, y_min = min(x_min, x), min(y_min, y)
        x_max, y_max = max(x_max, x), max(y_max, y)
    if x_max < 0:
        return None
    return x_min, y_min, x_max + 1, y_max + 1
