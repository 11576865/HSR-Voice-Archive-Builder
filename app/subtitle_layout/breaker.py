from __future__ import annotations


PROTECTED_PHRASES = (
    "let alone",
    "as soon as",
    "in order to",
    "even though",
    "right now",
    "at least",
    "kind of",
    "a lot of",
)


def tokenize_phrases(text: str) -> list[str]:
    """Keep common multi-word expressions together during wrapping."""
    words = text.split()
    result: list[str] = []
    index = 0

    while index < len(words):
        matched = None
        for phrase in PROTECTED_PHRASES:
            parts = phrase.split()
            if words[index:index + len(parts)] == parts:
                matched = phrase
                break
        if matched:
            result.append(matched)
            index += len(matched.split())
        else:
            result.append(words[index])
            index += 1

    return result


def split_text(text: str, max_width: int, measure) -> list[str]:
    """Break English at word boundaries and avoid splitting protected phrases."""
    lines: list[str] = []
    current = ""

    for token in tokenize_phrases(text):
        candidate = token if not current else f"{current} {token}"
        if measure(candidate) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = token

    if current:
        lines.append(current)

    return lines
