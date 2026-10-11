"""Deterministic, bounded candidate-time sampling for ASS animation inspection.

This is *sampled evidence*, never proof of an uncountable time continuum.
"""
from __future__ import annotations

import math
from typing import Any


MAX_SAMPLE_COUNT = 12
MAX_WORD_ALIGNMENTS = 500


def plan_temporal_samples(
    duration: float,
    *,
    word_alignments: list[Any] | None = None,
    enable_karaoke: bool = False,
    enable_kinetic: bool = False,
    fade_in_ms: int = 0,
    fade_out_ms: int = 0,
    enable_soft_entry: bool = False,
    soft_entry_ms: int = 0,
    sample_limit: int = 8,
) -> dict[str, object]:
    """Return bounded time instants and explicit omitted-candidate evidence.

    Each candidate represents a temporal event or a uniform time anchor.
    Sample selection retains the first/last/middle instants, prioritizes
    animation boundaries, then fills widest temporal gaps.
    """
    if not math.isfinite(duration) or not 0.25 <= duration <= 120:
        raise ValueError("Temporal sample duration out of range")
    if type(sample_limit) is not int or not 3 <= sample_limit <= MAX_SAMPLE_COUNT:
        raise ValueError("Temporal sample limit must be an integer from 3 to 12")
    end = round(duration - 0.01, 3)
    candidates: dict[float, set[str]] = {}

    def add(value: float, reason: str) -> None:
        if not math.isfinite(value):
            return
        time = round(min(end, max(0.0, value)), 3)
        candidates.setdefault(time, set()).add(reason)

    add(0, "event_start")
    add(end, "event_end")
    add(end / 2, "event_midpoint")
    for i in range(1, 17):
        add(end * i / 17, "stratified")

    if enable_kinetic:
        for value, label in (
            (fade_in_ms / 2000.0, "fade_in_mid"),
            (fade_in_ms / 1000.0, "fade_in_end"),
            (duration - fade_out_ms / 1000.0, "fade_out_start"),
            (duration - fade_out_ms / 2000.0, "fade_out_mid"),
        ):
            if 0 < value < end:
                add(value, label)
    if enable_soft_entry:
        for value, label in (
            (soft_entry_ms / 2000.0, "soft_entry_mid"),
            (soft_entry_ms / 1000.0, "soft_entry_end"),
        ):
            if 0 < value < end:
                add(value, label)

    alignments = word_alignments if isinstance(word_alignments, list) else []
    truncated = bool(enable_karaoke and len(alignments) > MAX_WORD_ALIGNMENTS)
    recognized_words = 0
    if enable_karaoke:
        for word in alignments[:MAX_WORD_ALIGNMENTS]:
            if not isinstance(word, dict):
                continue
            try:
                start, stop = float(word["start"]), float(word["end"])
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
            if not (math.isfinite(start) and math.isfinite(stop) and 0 <= start < stop <= duration + 0.01):
                continue
            recognized_words += 1
            add(start, "word_start")
            add((start + stop) / 2, "word_midpoint")
            add(stop, "word_end")

    ordered = sorted(candidates)
    chosen: set[float] = {0.0, end, round(end / 2, 3)}
    focus = [t for t in ordered if any(
        kind in candidates[t] for kind in (
            "fade_in_mid", "fade_out_mid", "soft_entry_mid"
        )
    )]
    for t in focus:
        if len(chosen) < sample_limit:
            chosen.add(t)
    word_events = [t for t in ordered if "word_start" in candidates[t] or "word_end" in candidates[t]]
    if word_events and len(chosen) < sample_limit:
        chosen.add(word_events[len(word_events) // 2])

    while len(chosen) < min(sample_limit, len(ordered)):
        remaining = [t for t in ordered if t not in chosen]
        if not remaining:
            break
        def score(t: float) -> tuple[float, float]:
            distance = min(abs(t - existing) for existing in chosen)
            priority = 1.15 if candidates[t].intersection({"word_start", "word_end", "fade_in_end", "fade_out_start", "soft_entry_end"}) else 1.0
            return (distance * priority, -t)
        chosen.add(max(remaining, key=score))

    times = sorted(chosen)
    return {
        "samples": [{"timestamp_seconds": t, "reasons": sorted(candidates[t])} for t in times],
        "candidate_count": len(ordered),
        "omitted_candidate_count": len(ordered) - len(times),
        "recognized_word_count": recognized_words,
        "word_alignment_input_truncated": truncated,
        "max_unsampled_gap_seconds": round(max((b - a for a, b in zip(times, times[1:])), default=0.0), 3),
        "sample_limit": sample_limit,
    }
