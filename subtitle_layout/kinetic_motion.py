from __future__ import annotations

import math


def generate_kinetic_tags(
    duration_seconds: float,
    *,
    base_entry_ms: int | None = None,
    base_exit_ms: int | None = None,
    max_portion: float = 0.25,
    initial_scale: int | None = None,
    peak_scale: int | None = None,
    exit_scale: int | None = None,
    x: int | float | None = None,
    y: int | float | None = None,
    entry_y_offset: int | float = 0,
    exit_y_offset: int | float = 0,
    voice_gap_seconds: float | None = None,
) -> str:
    """Generate ASS motion override tags for smooth spring easing entry/exit and physics displacement.

    Utilizes ASS tags:
      - \\pos(x, y) / \\move(x1, y1, x2, y2, t1, t2) for spatial positioning and displacement
      - \\fscx / \\fscy for scale transformation
      - \\fad(t_in, t_out) for opacity fading
      - \\t(t1, t2, accel, tags) for physics-inspired easing keyframes

    Parameters
    ----------
    duration_seconds : float
        Total display duration of the subtitle entry in seconds.
    base_entry_ms : int
        Standard duration for spring entry animation in milliseconds.
    base_exit_ms : int
        Standard duration for fade/shrink exit animation in milliseconds.
    max_portion : float
        Maximum fraction of total duration allowed for entry or exit animation.
    initial_scale : int
        Initial percentage scale before spring pop (e.g., 88%).
    peak_scale : int
        Overshoot percentage scale at the peak of spring entry (e.g., 106%).
    exit_scale : int
        Final percentage scale during shrink exit transition (e.g., 92%).
    x : int | float | None
        Target horizontal coordinate.
    y : int | float | None
        Target vertical coordinate.
    entry_y_offset : int | float
        Vertical pixel offset at the start of entry motion (e.g., +8 or -8).
    exit_y_offset : int | float
        Vertical pixel offset at the end of exit motion.

    Returns
    -------
    str
        ASS tag string (e.g., "\\pos(960,530)\\fscx88\\fscy88\\fad(200,200)...").
    """
    duration_ms = max(100, int(round(duration_seconds * 1000)))

    # Tiered Voice Gap Classification & Dynamic Parameter Tuning
    if voice_gap_seconds is not None:
        if voice_gap_seconds < 0.3:
            # Compact Mode (T_gap < 0.3s): Compressed fades (50-100ms), damped overshoot
            default_entry = 80
            default_exit = 80
            default_initial = 94
            default_peak = 103
            default_exit_s = 96
        elif voice_gap_seconds <= 1.0:
            # Normal Mode (0.3s <= T_gap <= 1.0s): Standard baseline fade (100-200ms)
            default_entry = 150
            default_exit = 150
            default_initial = 88
            default_peak = 106
            default_exit_s = 92
        else:
            # Spacious/Cinematic Mode (T_gap > 1.0s): Extended fade (200-400ms), cinematic motion
            default_entry = 300
            default_exit = 300
            default_initial = 82
            default_peak = 108
            default_exit_s = 88
    else:
        default_entry = 200
        default_exit = 200
        default_initial = 88
        default_peak = 106
        default_exit_s = 92

    entry_ms = default_entry if base_entry_ms is None else base_entry_ms
    exit_ms = default_exit if base_exit_ms is None else base_exit_ms
    init_scale = default_initial if initial_scale is None else initial_scale
    pk_scale = default_peak if peak_scale is None else peak_scale
    ex_scale = default_exit_s if exit_scale is None else exit_scale

    # Boundary Capping Constraints (30ms lower safety bound, 400ms upper cap)
    capped_entry_ms = max(30, min(400, entry_ms))
    capped_exit_ms = max(30, min(400, exit_ms))

    # Dynamically cap animation durations according to total duration
    max_anim_ms = max(30, int(math.floor(duration_ms * max_portion)))
    t_in = min(capped_entry_ms, max_anim_ms)
    t_out = min(capped_exit_ms, max_anim_ms)

    # Ensure entry and exit don't overlap
    if t_in + t_out > duration_ms:
        t_in = duration_ms // 2
        t_out = duration_ms // 2

    # Keyframe timing for spring entry overshoot
    # Phase 1: Initial pop to peak (overshoot)
    t1 = max(10, int(round(t_in * 0.6)))
    # Phase 2: Settle from peak back to 100% scale
    t2 = t_in

    # Phase 3: Exit shrink
    t_exit_start = max(t2, duration_ms - t_out)
    t_exit_end = duration_ms

    tags: list[str] = []

    # Positional displacement tags
    if x is not None and y is not None:
        rx, ry = round(x), round(y)
        if entry_y_offset != 0 and t_in > 0:
            start_y = round(y + entry_y_offset)
            tags.append(f"\\move({rx},{start_y},{rx},{ry},0,{t_in})")
        else:
            tags.append(f"\\pos({rx},{ry})")

    # Set initial scale
    tags.append(f"\\fscx{init_scale}\\fscy{init_scale}")

    # Set opacity fade in/out
    tags.append(f"\\fad({t_in},{t_out})")

    # Spring Entry Stage 1: Expand pop with ease-out acceleration exponent (accel=0.6)
    if t1 > 0:
        tags.append(f"\\t(0,{t1},0.6,\\fscx{pk_scale}\\fscy{pk_scale})")

    # Spring Entry Stage 2: Settle back to 100% with ease-in exponent (accel=1.4)
    if t2 > t1:
        tags.append(f"\\t({t1},{t2},1.4,\\fscx100\\fscy100)")

    # Exit Stage: Shrink to exit scale during fade out with accel=1.5
    if t_exit_end > t_exit_start:
        tags.append(f"\\t({t_exit_start},{t_exit_end},1.5,\\fscx{ex_scale}\\fscy{ex_scale})")

    return "".join(tags)
