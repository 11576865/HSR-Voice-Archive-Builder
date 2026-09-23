from __future__ import annotations

import math


def generate_kinetic_tags(
    duration_seconds: float,
    *,
    base_entry_ms: int = 200,
    base_exit_ms: int = 200,
    max_portion: float = 0.25,
    initial_scale: int = 88,
    peak_scale: int = 106,
    exit_scale: int = 92,
) -> str:
    """Generate ASS motion override tags for smooth spring easing entry/exit.

    Utilizes ASS tags:
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

    Returns
    -------
    str
        ASS tag string (e.g., "\\fscx88\\fscy88\\fad(200,200)...").
    """
    duration_ms = max(100, int(round(duration_seconds * 1000)))

    # Dynamically cap animation durations according to total duration
    max_anim_ms = max(30, int(math.floor(duration_ms * max_portion)))
    t_in = min(base_entry_ms, max_anim_ms)
    t_out = min(base_exit_ms, max_anim_ms)

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

    # Set initial scale
    tags.append(f"\\fscx{initial_scale}\\fscy{initial_scale}")

    # Set opacity fade in/out
    tags.append(f"\\fad({t_in},{t_out})")

    # Spring Entry Stage 1: Expand pop with ease-out acceleration exponent (accel=0.6)
    if t1 > 0:
        tags.append(f"\\t(0,{t1},0.6,\\fscx{peak_scale}\\fscy{peak_scale})")

    # Spring Entry Stage 2: Settle back to 100% with ease-in exponent (accel=1.4)
    if t2 > t1:
        tags.append(f"\\t({t1},{t2},1.4,\\fscx100\\fscy100)")

    # Exit Stage: Shrink to exit scale during fade out with accel=1.5
    if t_exit_end > t_exit_start:
        tags.append(f"\\t({t_exit_start},{t_exit_end},1.5,\\fscx{exit_scale}\\fscy{exit_scale})")

    return "".join(tags)
