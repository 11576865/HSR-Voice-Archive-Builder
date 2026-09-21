from __future__ import annotations

from dataclasses import dataclass
import unicodedata


@dataclass(frozen=True)
class SafeArea:
    width: int = 1920
    height: int = 1080
    horizontal_ratio: float = 0.10
    vertical_ratio: float = 0.05
    center_gap_ratio: float = 0.15

    @property
    def left(self) -> int:
        return round(self.width * self.horizontal_ratio)

    @property
    def right(self) -> int:
        return self.width - self.left

    @property
    def top(self) -> int:
        return round(self.height * self.vertical_ratio)

    @property
    def bottom(self) -> int:
        return self.height - self.top

    @property
    def center_gap(self) -> int:
        return round(self.height * self.center_gap_ratio)

    @property
    def text_width(self) -> int:
        return self.right - self.left


@dataclass(frozen=True)
class LayoutLine:
    text: str
    x: int
    y: int
    font_size: int


class SubtitleLayoutEngine:
    """Layout engine for bilingual black-screen ASS subtitles.

    Source language grows upward from the lower safe area.
    Chinese grows downward from the upper safe area.
    """

    def __init__(self, safe_area: SafeArea | None = None):
        self.safe = safe_area or SafeArea()

    @staticmethod
    def glyph_width(char: str, font_size: int) -> float:
        if unicodedata.east_asian_width(char) in {"W", "F"}:
            return font_size
        return font_size * 0.55

    def wrap(self, text: str, font_size: int) -> list[str]:
        lines: list[str] = []
        current = ""
        width = 0.0

        for char in text:
            char_width = self.glyph_width(char, font_size)
            if current and width + char_width > self.safe.text_width:
                lines.append(current)
                current = char
                width = char_width
            else:
                current += char
                width += char_width

        if current:
            lines.append(current)
        return lines

    def fit(self, text: str, initial_size: int, minimum_size: int, max_lines: int) -> tuple[list[str], int]:
        for reduction in range(0, 100, 5):
            size = initial_size - reduction
            if size < minimum_size:
                break
            lines = self.wrap(text, size)
            if len(lines) <= max_lines:
                return lines, size
        return self.wrap(text, minimum_size), minimum_size

    def place_bottom_anchored(self, lines: list[str], font_size: int, line_gap: int = 8) -> list[LayoutLine]:
        result = []
        y = self.safe.bottom
        for line in reversed(lines):
            result.append(LayoutLine(line, self.safe.width // 2, y, font_size))
            y -= font_size + line_gap
        return list(reversed(result))

    def place_top_anchored(self, lines: list[str], font_size: int, line_gap: int = 8) -> list[LayoutLine]:
        result = []
        y = self.safe.top
        for line in lines:
            result.append(LayoutLine(line, self.safe.width // 2, y, font_size))
            y += font_size + line_gap
        return result
