from __future__ import annotations

import math


def generate_kinetic_tags(
    duration_seconds: float,
    *,
    fade_in_ms: int = 200,
    fade_out_ms: int = 200,
    x: int | float | None = None,
    y: int | float | None = None,
    entry_y_offset: int | float = 0,
    exit_y_offset: int | float = 0,
    voice_gap_seconds: float | None = None,
    base_entry_ms: int | None = None,
    base_exit_ms: int | None = None,
    max_portion: float = 0.25,
    initial_scale: int | None = None,
    peak_scale: int | None = None,
    exit_scale: int | None = None,
) -> str:
    """Generate clean ASS position and opacity fade override tags (\\pos/\\move, \\fad).

    Physical overshoot and scale bounce transforms (\\fscx/\\fscy scale jumps) have been
    deprecated and removed in favor of clean audio-aware fade-in/fade-out transitions.
    """
    duration_ms = max(100, int(round(duration_seconds * 1000)))

    # Voice Gap aware dynamic fade duration adjustments
    if voice_gap_seconds is not None:
        if voice_gap_seconds < 0.3:
            default_fade = 80
        elif voice_gap_seconds <= 1.0:
            default_fade = 150
        else:
            default_fade = 300
        in_ms = default_fade if base_entry_ms is None else base_entry_ms
        out_ms = default_fade if base_exit_ms is None else base_exit_ms
    else:
        in_ms = fade_in_ms if base_entry_ms is None else base_entry_ms
        out_ms = fade_out_ms if base_exit_ms is None else base_exit_ms

    max_anim_ms = max(30, int(math.floor(duration_ms * max_portion)))
    t_in = min(max(0, in_ms), max_anim_ms)
    t_out = min(max(0, out_ms), max_anim_ms)

    if t_in + t_out > duration_ms:
        t_in = duration_ms // 2
        t_out = duration_ms // 2

    tags: list[str] = []

    # Positional displacement tags
    if x is not None and y is not None:
        rx, ry = round(x), round(y)
        if entry_y_offset != 0 and t_in > 0:
            start_y = round(y + entry_y_offset)
            tags.append(f"\\move({rx},{start_y},{rx},{ry},0,{t_in})")
        else:
            tags.append(f"\\pos({rx},{ry})")

    # Clean Opacity Fade Tag
    if t_in > 0 or t_out > 0:
        tags.append(f"\\fad({t_in},{t_out})")

    return "".join(tags)
