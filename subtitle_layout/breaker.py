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

# Pre-compiled regex patterns for protected phrases to avoid repeated compilation overhead during line breaking
_PROTECTED_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (f"__PROTECTED_{idx}__", phrase, re.compile(re.escape(phrase), re.IGNORECASE))
    for idx, phrase in enumerate(PROTECTED_PHRASES)
)


def _tokenize_text(text: str) -> list[str]:
    """Tokenize text preserving protected phrases, words, spaces, and punctuation."""
    if not text:
        return []

    normalized = text
    phrase_map: dict[str, str] = {}
    for placeholder, _phrase, pattern in _PROTECTED_PATTERNS:
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


PUNCTUATION_CHARS = {".", ",", "!", "?", "，", "。", "！", "？"}
ALL_PUNCTUATION = {".", ",", "!", "?", "，", "。", "！", "？", ";", ":", "；", "：", "—", "…", "-", "\"", "'"}


def _protected_phrase_spans(text: str) -> list[tuple[int, int]]:
    """Return character index ranges (start, end) for protected phrases in text."""
    spans: list[tuple[int, int]] = []
    text_lower = text.lower()
    for _ph, _orig, pattern in _PROTECTED_PATTERNS:
        for match in pattern.finditer(text_lower):
            spans.append((match.start(), match.end()))
    return spans


def _get_candidate_split_points(text: str) -> list[int]:
    """Return a sorted list of candidate split character indices in text."""
    candidates: set[int] = set()
    n = len(text)
    for i in range(1, n):
        c_prev = text[i - 1]
        c_curr = text[i]
        # Candidate split right after space or at space boundary
        if c_prev.isspace() or c_curr.isspace():
            candidates.add(i)
        # Candidate split right after punctuation
        elif c_prev in ALL_PUNCTUATION:
            candidates.add(i)
        # Candidate split between CJK characters
        elif ord(c_prev) > 127 or ord(c_curr) > 127:
            candidates.add(i)

    # Fallback: if no natural candidates, consider all character boundaries
    if not candidates and n > 1:
        candidates = set(range(1, n))

    return sorted(candidates)


def _score_split_point(text: str, k: int, protected_spans: list[tuple[int, int]]) -> tuple[float, float, float]:
    """Calculate (boundary_score, protected_penalty, imbalance_penalty) for split index k."""
    l1 = text[:k].strip()
    l2 = text[k:].strip()

    # 1. Boundary score
    if l1 and l1[-1] in PUNCTUATION_CHARS:
        boundary_score = 10.0
    elif l1 and l1[-1] in ALL_PUNCTUATION:
        boundary_score = 10.0
    elif text[k - 1].isspace() or text[k].isspace():
        boundary_score = 5.0
    elif ord(text[k - 1]) > 127 or ord(text[k]) > 127:
        boundary_score = 5.0
    else:
        boundary_score = 0.0

    # 2. Protected phrase penalty
    protected_penalty = 0.0
    for p_start, p_end in protected_spans:
        if p_start < k < p_end:
            protected_penalty -= 100.0

    # 3. Line imbalance penalty
    imbalance_penalty = -abs(len(l1) - len(l2)) * 2.0

    return boundary_score, protected_penalty, imbalance_penalty


def break_line(
    text: str,
    max_width: float,
    font_size: int,
) -> list[str]:
    """Rule-based line breaking with scoring and backtracking.

    Scoring Rules:
    - Punctuation Boundary Score: +10
    - Whitespace / Word Boundary Score: +5
    - Protected Phrase Splitting Penalty: -100
    - Line Imbalance Penalty: - |len(line1) - len(line2)| * 2
    """
    cleaned = text.strip()
    if not cleaned:
        return []

    if measure_text_width(cleaned, font_size) <= max_width:
        return [cleaned]

    protected_spans = _protected_phrase_spans(cleaned)
    candidate_k = _get_candidate_split_points(cleaned)

    # First, try to find a 2-line split where BOTH lines fit within max_width
    valid_two_line_candidates: list[tuple[float, int, str, str]] = []

    for k in candidate_k:
        l1 = cleaned[:k].strip()
        l2 = cleaned[k:].strip()
        if not l1 or not l2:
            continue

        w1 = measure_text_width(l1, font_size)
        w2 = measure_text_width(l2, font_size)

        if w1 <= max_width and w2 <= max_width:
            b_score, p_pen, imb_pen = _score_split_point(cleaned, k, protected_spans)
            total_score = b_score + p_pen + imb_pen
            valid_two_line_candidates.append((total_score, k, l1, l2))

    if valid_two_line_candidates:
        # Pick the candidate with the highest total score
        valid_two_line_candidates.sort(key=lambda x: (x[0], -abs(len(x[2]) - len(x[3]))), reverse=True)
        best = valid_two_line_candidates[0]
        return [best[2], best[3]]

    # If no 2-line split allows both lines to fit within max_width,
    # find the best split index k that makes line1 fit within max_width
    fit_candidates: list[tuple[float, int, str, str]] = []
    for k in candidate_k:
        l1 = cleaned[:k].strip()
        l2 = cleaned[k:].strip()
        if not l1 or not l2:
            continue

        w1 = measure_text_width(l1, font_size)
        if w1 <= max_width:
            b_score, p_pen, _ = _score_split_point(cleaned, k, protected_spans)
            # Prefer longer line1 to maximize progress while keeping line1 valid
            fill_score = w1 / max_width * 10.0
            total_score = b_score + p_pen + fill_score
            fit_candidates.append((total_score, k, l1, l2))

    if fit_candidates:
        fit_candidates.sort(key=lambda x: x[0], reverse=True)
        best = fit_candidates[0]
        rest_lines = break_line(best[3], max_width, font_size)
        return [best[2]] + rest_lines

    # Last resort fallback: forced character splitting when a single word/character exceeds max_width
    sub_line = ""
    for char in cleaned:
        if measure_text_width(sub_line + char, font_size) <= max_width:
            sub_line += char
        else:
            if sub_line:
                break
            sub_line = char
    if sub_line and len(sub_line) < len(cleaned):
        rest = cleaned[len(sub_line):].strip()
        return [sub_line] + (break_line(rest, max_width, font_size) if rest else [])

    return [cleaned]
