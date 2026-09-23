from __future__ import annotations

import math


def classify_gap_mode(gap_seconds: float | None) -> str:
    """Classify silence gap T_gap into compact (<0.3s), spacious (>1.0s), or normal mode."""
    if gap_seconds is None:
        return "normal"
    if gap_seconds < 0.3:
        return "compact"
    if gap_seconds > 1.0:
        return "spacious"
    return "normal"


def compute_gap_fade_ms(
    gap_seconds: float | None,
    base_fade_ms: int = 200,
) -> int:
    """Compute dynamic subtitle fade duration in milliseconds based on silence gap T_gap.

    - Compact Mode (T_gap < 0.3s): 50ms - 100ms
    - Normal Mode (0.3s <= T_gap <= 1.0s): 100ms - 200ms
    - Spacious Mode (T_gap > 1.0s): 200ms - 400ms
    """
    if gap_seconds is None:
        return base_fade_ms
    g = max(0.0, float(gap_seconds))
    if g < 0.3:
        return max(50, min(100, int(round(50.0 + (g / 0.3) * 50.0))))
    elif g > 1.0:
        return min(400, int(round(200.0 + (g - 1.0) * 200.0)))
    else:
        return int(round(100.0 + ((g - 0.3) / 0.7) * 100.0))


def generate_kinetic_tags(
    duration_seconds: float,
    *,
    gap_before: float | None = None,
    gap_after: float | None = None,
    base_entry_ms: int = 200,
    base_exit_ms: int = 200,
    max_portion: float = 0.25,
    initial_scale: int = 88,
    peak_scale: int = 106,
    exit_scale: int = 92,
) -> str:
    """Generate ASS motion override tags for dynamic fade and spring easing entry/exit.

    Utilizes ASS tags:
      - \\fscx / \\fscy for scale transformation
      - \\fad(t_in, t_out) for opacity fading
      - \\t(t1, t2, accel, tags) for physics-inspired easing keyframes

    Parameters
    ----------
    duration_seconds : float
        Total display duration of the subtitle entry in seconds.
    gap_before : float | None
        Silence gap before the dialogue line (in seconds) for entry adaptation.
    gap_after : float | None
        Silence gap after the dialogue line (in seconds) for exit adaptation.
    base_entry_ms : int
        Fallback duration for spring entry animation in milliseconds if gap_before is None.
    base_exit_ms : int
        Fallback duration for shrink exit animation in milliseconds if gap_after is None.
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

    raw_t_in = compute_gap_fade_ms(gap_before, base_fade_ms=base_entry_ms)
    raw_t_out = compute_gap_fade_ms(gap_after, base_fade_ms=base_exit_ms)

    calc_initial_scale = initial_scale
    calc_peak_scale = peak_scale
    if gap_before is not None and gap_before < 0.3:
        ratio = max(0.0, min(1.0, gap_before / 0.3))
        calc_initial_scale = int(round(96.0 - ratio * (96.0 - float(initial_scale))))
        calc_peak_scale = int(round(101.0 + ratio * (float(peak_scale) - 101.0)))

    calc_exit_scale = exit_scale
    if gap_after is not None and gap_after < 0.3:
        ratio = max(0.0, min(1.0, gap_after / 0.3))
        calc_exit_scale = int(round(96.0 - ratio * (96.0 - float(exit_scale))))

    # Dynamically cap animation durations according to total duration
    max_anim_ms = max(30, int(math.floor(duration_ms * max_portion)))
    t_in = min(raw_t_in, max_anim_ms)
    t_out = min(raw_t_out, max_anim_ms)

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
    tags.append(f"\\fscx{calc_initial_scale}\\fscy{calc_initial_scale}")

    # Set opacity fade in/out
    tags.append(f"\\fad({t_in},{t_out})")

    # Spring Entry Stage 1: Expand pop with ease-out acceleration exponent (accel=0.6)
    if t1 > 0:
        tags.append(f"\\t(0,{t1},0.6,\\fscx{calc_peak_scale}\\fscy{calc_peak_scale})")

    # Spring Entry Stage 2: Settle back to 100% with ease-in exponent (accel=1.4)
    if t2 > t1:
        tags.append(f"\\t({t1},{t2},1.4,\\fscx100\\fscy100)")

    # Exit Stage: Shrink to exit scale during fade out with accel=1.5
    if t_exit_end > t_exit_start:
        tags.append(f"\\t({t_exit_start},{t_exit_end},1.5,\\fscx{calc_exit_scale}\\fscy{calc_exit_scale})")

    return "".join(tags)
