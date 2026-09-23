from __future__ import annotations

import jieba.posseg as pseg

PUNCTUATION_SPLITS = ("，", "。", "！", "？", "；", "——", "~", ",", ";", "!", "?")
FORBIDDEN_LINE_START_PUNCT = ("！", "？", "。", "」", "!", "?", ".", ")", "]", "}", "”", "’", "；", "，", ",")


def _fallback_middle_split(words: list[tuple[str, str]], min_chars_line: int, total_len: int) -> int:
    target_split = total_len // 2
    best_idx = -1
    min_diff = total_len
    curr_len = 0

    for word, _flag in words:
        curr_len += len(word)
        if curr_len >= min_chars_line and (total_len - curr_len) >= min_chars_line:
            diff = abs(curr_len - target_split)
            if diff < min_diff:
                min_diff = diff
                best_idx = curr_len

    if best_idx != -1:
        return best_idx

    # Absolute character fallback near middle
    if total_len >= min_chars_line * 2:
        return target_split
    return -1


def split_chinese_semantic(
    text: str, max_chars_per_line: int = 22, min_chars_line: int = 4
) -> str:
    """Split Chinese text into lines preserving semantic boundaries and preventing orphans."""
    cleaned = text.strip()
    if not cleaned:
        return cleaned

    total_len = len(cleaned)
    if max_chars_per_line > 0 and total_len <= max_chars_per_line:
        return cleaned

    target_split = total_len // 2

    # 1. Punctuation Boundaries (Highest Priority)
    best_punct_idx = -1
    min_punct_diff = total_len

    for idx, char in enumerate(cleaned):
        if char in PUNCTUATION_SPLITS:
            split_at = idx + 1
            l1_len = split_at
            l2_len = total_len - split_at
            if l1_len >= min_chars_line and l2_len >= min_chars_line:
                diff = abs(l1_len - target_split)
                if diff < min_punct_diff:
                    min_punct_diff = diff
                    best_punct_idx = split_at

    best_idx = -1
    if best_punct_idx != -1:
        best_idx = best_punct_idx
    else:
        # 2. Grammar Component Boundaries (Secondary Priority: POS tagging)
        words = list(pseg.cut(cleaned))
        min_diff = total_len
        curr_len = 0

        for word, flag in words:
            curr_len += len(word)
            if curr_len >= min_chars_line and (total_len - curr_len) >= min_chars_line:
                if flag in ("c", "p", "u", "x"):  # Conjunction, Preposition, Particle, Punctuation
                    diff = abs(curr_len - target_split)
                    if diff < min_diff:
                        min_diff = diff
                        best_idx = curr_len

        # Fallback to middle word boundary
        if best_idx == -1:
            best_idx = _fallback_middle_split(words, min_chars_line, total_len)

    if best_idx == -1 or best_idx <= 0 or best_idx >= total_len:
        return cleaned

    line1 = cleaned[:best_idx].rstrip("，, ")
    line2 = cleaned[best_idx:].lstrip("，, ")

    # Kinsoku Shori / Punctuation Pushing:
    while line2 and line2[0] in FORBIDDEN_LINE_START_PUNCT:
        line1 += line2[0]
        line2 = line2[1:].lstrip()

    if not line2:
        return line1

    # Orphan protection: check if line2 or line1 is shorter than min_chars_line
    if len(line1) < min_chars_line or len(line2) < min_chars_line:
        words1 = list(pseg.cut(line1))
        if len(words1) > 1:
            last_word = words1[-1][0]
            line1_cand = line1[:-len(last_word)].rstrip("，, ")
            line2_cand = last_word + line2
            if len(line1_cand) >= min_chars_line and len(line2_cand) >= min_chars_line:
                line1, line2 = line1_cand, line2_cand
            else:
                return cleaned
        else:
            return cleaned

    return f"{line1}\\N{line2}"
