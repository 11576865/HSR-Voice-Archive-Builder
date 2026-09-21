from dataclasses import dataclass


@dataclass
class LayoutConfig:
    width: int = 1920
    height: int = 1080
    horizontal_margin: float = 0.10
    vertical_margin: float = 0.05
    center_gap: int = 120
    font_size: int = 60
    min_scale_steps: tuple[int, ...] = (100, 95, 90, 85)

    @property
    def left(self):
        return int(self.width * self.horizontal_margin)

    @property
    def right(self):
        return int(self.width * (1 - self.horizontal_margin))

    @property
    def top(self):
        return int(self.height * self.vertical_margin)

    @property
    def bottom(self):
        return int(self.height * (1 - self.vertical_margin))


@dataclass
class SubtitleLine:
    text: str
    x: int
    y: int
    size: int
    layer: int


class SubtitleLayoutEngine:
    """Geometry-only ASS layout engine.

    Source language grows upward from the bottom safe area.
    Target language grows downward from the top safe area.
    """

    def __init__(self, config: LayoutConfig | None = None):
        self.config = config or LayoutConfig()

    def layout_bottom(self, lines: list[str], size: int | None = None):
        size = size or self.config.font_size
        result = []
        y = self.config.bottom
        for line in reversed(lines):
            result.append(SubtitleLine(line, self.config.width // 2, y, size, 0))
            y -= int(size * 1.25)
        return list(reversed(result))

    def layout_top(self, lines: list[str], size: int | None = None):
        size = size or self.config.font_size
        result = []
        y = self.config.top
        for line in lines:
            result.append(SubtitleLine(line, self.config.width // 2, y, size, 1))
            y += int(size * 1.25)
        return result

    def collision(self, top_lines, bottom_lines):
        if not top_lines or not bottom_lines:
            return False
        top_end = max(x.y for x in top_lines)
        bottom_start = min(x.y for x in bottom_lines)
        return top_end + self.config.center_gap >= bottom_start
