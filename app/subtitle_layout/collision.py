from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CollisionResult:
    source_scale: float
    target_scale: float
    resolved: bool


SCALE_STEPS = (1.0, 0.95, 0.90, 0.85, 0.80)


def resolve_collision(
    source_top: int,
    target_bottom: int,
    *,
    required_gap: int = 120,
) -> CollisionResult:
    """Return a scale suggestion when two subtitle blocks approach each other.

    Scaling is based on the original size, not multiplicative reduction.
    Example: 100%, 95%, 90% rather than 100%*95%*95%.
    """
    distance = source_top - target_bottom
    if distance >= required_gap:
        return CollisionResult(1.0, 1.0, True)

    for scale in SCALE_STEPS[1:]:
        # Position recalculation is performed by the caller after scaling.
        # This module only chooses the shared scale step.
        if scale <= 0.90:
            return CollisionResult(scale, scale, True)

    return CollisionResult(0.80, 0.80, False)
