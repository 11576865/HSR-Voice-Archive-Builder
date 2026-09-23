from __future__ import annotations

import functools
import unicodedata

try:
    from PIL import ImageFont
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

_FONT_CACHE: dict[tuple[str | None, int], object] = {}


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


@functools.lru_cache(maxsize=2048)
def _pil_measure_text_width(text: str, font_path: str | None, font_size: int) -> float | None:
    if not _HAS_PIL:
        return None
    key = (font_path, font_size)
    if key not in _FONT_CACHE:
        font_obj = None
        if font_path:
            try:
                font_obj = ImageFont.truetype(font_path, size=font_size)
            except Exception:
                pass
        if font_obj is None:
            try:
                font_obj = ImageFont.load_default(size=font_size)
            except Exception:
                try:
                    font_obj = ImageFont.load_default()
                except Exception:
                    font_obj = None
        _FONT_CACHE[key] = font_obj

    font = _FONT_CACHE[key]
    if font is None:
        return None

    try:
        if hasattr(font, "getlength"):
            return float(font.getlength(text))
        elif hasattr(font, "getbbox"):
            bbox = font.getbbox(text)
            return float(bbox[2] - bbox[0])
    except Exception:
        pass
    return None


def measure_text_width(text: str, font_size: int, font_path: str | None = None) -> float:
    """Measure total text width in pixels using PIL font metrics if available, falling back to ratio sum."""
    if not text:
        return 0.0
    if font_path:
        pil_width = _pil_measure_text_width(text, font_path, font_size)
        if pil_width is not None and pil_width > 0:
            return pil_width
    return sum(map(char_width_ratio, text)) * font_size


def measure_line_height(font_size: int) -> float:
    return font_size * 1.25


def clear_measure_cache() -> None:
    """Explicitly invalidate LRU caches and font metric cache to prevent memory leaks."""
    char_width_ratio.cache_clear()
    _pil_measure_text_width.cache_clear()
    _FONT_CACHE.clear()
