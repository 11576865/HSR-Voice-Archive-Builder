from __future__ import annotations

import re
from typing import Sequence

from .measure import measure_text_width

PROTECTED_PHRASES: tuple[str, ...] = (
    "let alone",
    "as soon as",
    "in order to",
    "even though",
    "right now",
    "at least",
    "kind of",
    "a lot of",
)


def _tokenize_text(text: str) -> list[str]:
    """Tokenize text preserving protected phrases, words, spaces, and punctuation."""
    if not text:
        return []

    normalized = text
    phrase_map: dict[str, str] = {}
    for idx, phrase in enumerate(PROTECTED_PHRASES):
        placeholder = f"__PROTECTED_{idx}__"
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        found = pattern.findall(normalized)
        for original in found:
            phrase_map[placeholder] = original
            normalized = pattern.sub(placeholder, normalized, count=1)

    raw_tokens: list[str] = []
    current_word: list[str] = []

    def flush_word():
        if current_word:
            raw_tokens.append("".join(current_word))
            current_word.clear()

    for char in normalized:
        code = ord(char)
        is_cjk = (
            0x4E00 <= code <= 0x9FFF
            or 0x3400 <= code <= 0x4DBF
            or 0x3000 <= code <= 0x303F
            or 0x3040 <= code <= 0x309F
            or 0x30A0 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF
            or 0xFF00 <= code <= 0xFFEF
        )
        if is_cjk:
            flush_word()
            raw_tokens.append(char)
        elif char.isspace():
            flush_word()
            raw_tokens.append(char)
        elif char in ",.?!;:，。！？；：—…-\"':":
            flush_word()
            raw_tokens.append(char)
        else:
            current_word.append(char)
    flush_word()

    tokens: list[str] = []
    for tok in raw_tokens:
        restored = tok
        for ph, orig in phrase_map.items():
            if ph in restored:
                restored = restored.replace(ph, orig)
        tokens.append(restored)

    return tokens


def break_line(
    text: str,
    max_width: float,
    font_size: int,
) -> list[str]:
    """Rule-based line breaking adhering to priorities:
    1. Punctuation
    2. Spaces and word boundaries
    3. Protected phrases
    4. Forced splitting when necessary
    """
    cleaned = text.strip()
    if not cleaned:
        return []

    if measure_text_width(cleaned, font_size) <= max_width:
        return [cleaned]

    tokens = _tokenize_text(cleaned)
    lines: list[str] = []
    current_line = ""

    for token in tokens:
        test_line = current_line + token
        if measure_text_width(test_line.strip(), font_size) <= max_width:
            current_line = test_line
        else:
            if current_line.strip():
                lines.append(current_line.strip())
                current_line = token.lstrip() if token.isspace() else token
            else:
                token_str = token
                sub_token = ""
                for char in token_str:
                    if measure_text_width(sub_token + char, font_size) <= max_width:
                        sub_token += char
                    else:
                        if sub_token:
                            lines.append(sub_token)
                        sub_token = char
                if sub_token:
                    current_line = sub_token

    if current_line.strip():
        lines.append(current_line.strip())

    return lines
