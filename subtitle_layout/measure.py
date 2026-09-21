from __future__ import annotations

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


def char_width_ratio(char: str) -> float:
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


def measure_text_width(text: str, font_size: int) -> float:
    return sum(char_width_ratio(c) * font_size for c in text)


def measure_line_height(font_size: int) -> float:
    return font_size * 1.25
