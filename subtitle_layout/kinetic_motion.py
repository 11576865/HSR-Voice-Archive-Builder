from __future__ import annotations

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
    max_portion: float = 1.0,
    initial_scale: int | None = None,
    peak_scale: int | None = None,
    exit_scale: int | None = None,
) -> str:
    """Generate clean ASS position and opacity fade override tags (\\pos/\\move, \\fad).

    Physical overshoot and scale bounce transforms (\\fscx/\\fscy scale jumps) have been
    deprecated and removed in favor of clean audio-aware fade-in/fade-out transitions.
    """
    # The source-aligned event already includes fade margins. Do not silently
    # replace manual durations with 80/150/300ms gap presets or compress them
    # to fit inside spoken time. Project validation guards real gap budgets.
    # The obsolete voice_gap_seconds/max_portion args remain for API compat.
    t_in = max(0, int(base_entry_ms if base_entry_ms is not None else fade_in_ms))
    t_out = max(0, int(base_exit_ms if base_exit_ms is not None else fade_out_ms))

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
