from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssLine:
    layer: int
    start: str
    end: str
    style: str
    x: int
    y: int
    text: str
    font_size: int


def write_line(line: AssLine) -> str:
    return (
        f"Dialogue: {line.layer},{line.start},{line.end},{line.style},,0,0,0,,"
        f"{{\\an8\\pos({line.x},{line.y})\\fs{line.font_size}}}{line.text}"
    )


def write_lines(lines: list[AssLine]) -> str:
    return "\n".join(write_line(line) for line in lines)
