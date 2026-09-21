from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SafeArea:
    canvas_width: int = 1920
    canvas_height: int = 1080
    margin_left_percent: float = 0.10
    margin_right_percent: float = 0.10
    margin_top_percent: float = 0.05
    margin_bottom_percent: float = 0.05

    @property
    def margin_left(self) -> int:
        return round(self.canvas_width * self.margin_left_percent)

    @property
    def margin_right(self) -> int:
        return round(self.canvas_width * self.margin_right_percent)

    @property
    def margin_top(self) -> int:
        return round(self.canvas_height * self.margin_top_percent)

    @property
    def margin_bottom(self) -> int:
        return round(self.canvas_height * self.margin_bottom_percent)

    @property
    def x_min(self) -> int:
        return self.margin_left

    @property
    def x_max(self) -> int:
        return self.canvas_width - self.margin_right

    @property
    def max_printable_width(self) -> int:
        return self.x_max - self.x_min

    @property
    def y_min(self) -> int:
        return self.margin_top

    @property
    def y_max(self) -> int:
        return self.canvas_height - self.margin_bottom

    @property
    def max_printable_height(self) -> int:
        return self.y_max - self.y_min


DEFAULT_SAFE_AREA = SafeArea()
