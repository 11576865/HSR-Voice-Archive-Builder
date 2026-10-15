"""Project-aware fade budgeting using the shortest real adjacent PCM voice gap.

The fade envelope surrounds the *voice*, not SRT's speech timestamps.
A complete between-voice silence is split into outgoing fade, clear air and
incoming fade. Never use the larger cross-chapter gaps to loosen a project limit.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=24)
def _shortest_manifest_gap(path: str, mtime_ns: int, size: int) -> int | None:
    del mtime_ns, size
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return None
    minimum = None
    previous = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if previous is not None:
            if previous.get("audio_end_seconds") is not None and entry.get("start_seconds") is not None:
                gap = max(0, round((float(entry["start_seconds"]) - float(previous["audio_end_seconds"])) * 1000))
                # Smallest across all adjacent clips: chapter gaps normally are
                # longer and cannot raise the minimum. If a chapter gap is
                # configured *shorter*, it must still guard against overlap.
                minimum = gap if minimum is None else min(minimum, gap)
        previous = entry
    return minimum


def fade_budget(config: Any) -> dict[str, Any]:
    # Before build (or for single-cue projects), use the *smaller* of
    # configured same-group and chapter gaps, never the larger as a budget.
    gap_ms = max(0, round(min(float(config.same_group_gap), float(config.group_gap)) * 1000))
    source = "configured_shorter_gap"
    output = Path(config.output_dir)
    if not output.is_absolute():
        output = Path(config.root) / output
    manifest = output / "manifest.json"
    if manifest.is_file() and not manifest.is_symlink():
        try:
            stats = manifest.stat()
            actual = _shortest_manifest_gap(str(manifest), stats.st_mtime_ns, stats.st_size)
            if actual is not None:
                gap_ms = actual
                source = "actual_minimum_adjacent_pcm"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    # HSR's current authoring contract has one symmetric Fade duration.
    # Legacy projects may retain an older unequal fade_out_ms in their JSON,
    # but preview/ASS use the incoming value on both sides.
    incoming = int(config.subtitle_fade_in_ms)
    outgoing = incoming
    ratio = (incoming + outgoing) / gap_ms if gap_ms else (
        float("inf") if incoming + outgoing > 0 else 0.0
    )
    return {
        "min_gap_ms": gap_ms,
        "source": source,
        "fade_total_ms": incoming + outgoing,
        "remaining_clear_ms": max(0, gap_ms - incoming - outgoing),
        "ratio": ratio if ratio != float("inf") else None,
        "warning": ratio > 0.60,
        "exceeds": ratio > 1.0,
        "warning_threshold": 0.60,
        "blocking_threshold": 1.0,
    }


def validate_fade_update(
    config: Any, fade_in_ms: int, fade_out_ms: int, *,
    enable_kinetic: bool = True,
) -> dict[str, Any]:
    if fade_in_ms < 0 or fade_out_ms < 0:
        raise ValueError("淡入淡出时间不能为负数")
    data = fade_budget(config)
    incoming = int(fade_in_ms)
    outgoing = int(fade_out_ms)
    proposed = incoming + outgoing
    old = int(config.subtitle_fade_in_ms) * 2
    if enable_kinetic and proposed > data["min_gap_ms"] and proposed > old:
        raise ValueError(
            "淡出 + 后淡入不能超过项目最短相邻语音间隔 "
            f'{data["min_gap_ms"]} ms；当前请求合计 {proposed} ms，'
            "请缩短淡入或淡出。"
        )
    return data
