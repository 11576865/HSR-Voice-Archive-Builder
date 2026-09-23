from __future__ import annotations

from .measure import measure_text_width

PLAY_RES_X: int = 1920
PLAY_RES_Y: int = 1080
MARGIN_X: int = 140
W_MAX: float = float(PLAY_RES_X - 2 * MARGIN_X)  # 1640.0
TARGET_FILL_RATIO: float = 0.82
W_TARGET: float = W_MAX * TARGET_FILL_RATIO  # ~1344.8


def calculate_target_font_size(
    text: str,
    base_font_size: int = 48,
    font_path: str | None = None,
    target_fill_ratio: float = TARGET_FILL_RATIO,
    max_width: float = W_MAX,
) -> tuple[int, float]:
    """Calculate target font size and scale factor based on pixel metrics and target fill ratio."""
    cleaned = text.strip()
    if not cleaned:
        return base_font_size, 1.0

    lines = cleaned.split(r"\N") if r"\N" in cleaned else cleaned.splitlines()
    raw_widths = [measure_text_width(line.strip(), base_font_size, font_path) for line in lines if line.strip()]
    max_w_raw = max(raw_widths) if raw_widths else 0.0

    if max_w_raw <= 0:
        return base_font_size, 1.0

    w_target = max_width * target_fill_ratio
    scale = w_target / max_w_raw
    s_calc = round(base_font_size * scale)

    return s_calc, scale
