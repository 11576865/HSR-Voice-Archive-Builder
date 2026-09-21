PROTECTED_PHRASES = {
    "let alone",
    "as soon as",
    "in order to",
    "even though",
    "right now",
    "at least",
}


def split_text(text: str, max_chars: int = 32) -> list[str]:
    """Basic word-aware splitter. Detailed phrase scoring will follow."""
    words = text.split()
    lines = []
    current = []
    size = 0
    for word in words:
        if current and size + len(word) + 1 > max_chars:
            lines.append(" ".join(current))
            current = [word]
            size = len(word)
        else:
            current.append(word)
            size += len(word) + 1
    if current:
        lines.append(" ".join(current))
    return lines
