from __future__ import annotations

import functools
import unicodedata


def is_cjk_char(char: str) -> bool:
    if not char:
        return False
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x20000 <= code <= 0x2A6DF
        or 0x3000 <= code <= 0x303F
        or 0x3040 <= code <= 0x309F
        or 0x30A0 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
        or 0xFF00 <= code <= 0xFFEF
    )


def _uncached_char_width_ratio(char: str) -> float:
    if is_cjk_char(char):
        return 1.0
    if char == " ":
        return 0.3
    if char in "iI1l!|;,.'\"":
        return 0.35
    if char in "fjt()[]{}":
        return 0.45
    if char in "mwMW@#%&":
        return 0.8
    if char.isupper():
        return 0.65
    return 0.52


# Precompute lookup table for ASCII characters (ord 0..127) for fast character width calculation
_ASCII_WIDTH_RATIOS: tuple[float, ...] = tuple(_uncached_char_width_ratio(chr(i)) for i in range(128))


@functools.lru_cache(maxsize=2048)
def char_width_ratio(char: str) -> float:
    """Return width ratio for character, with O(1) ASCII table lookup and LRU caching for unicode chars."""
    if not char:
        return 0.35
    code = ord(char)
    if code < 128:
        return _ASCII_WIDTH_RATIOS[code]
    return _uncached_char_width_ratio(char)


def measure_text_width(text: str, font_size: int) -> float:
    """Measure total text width in pixels using sum(map(...)) for fast C-level iteration."""
    if not text:
        return 0.0
    return sum(map(char_width_ratio, text)) * font_size


def measure_line_height(font_size: int) -> float:
    return font_size * 1.25
