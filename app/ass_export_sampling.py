"""Risk-aware, bounded time sampling of actual exported ASS Dialogue events.

Candidate generation observes exported override tags, not a guessed
PreviewEntry or unsaved editor state. Samples are never all-time evidence.
"""
from __future__ import annotations

import math
import re
from typing import Any

MAX_ASS_DIALOGUES = 200000
MAX_EXPORT_SAMPLES = 12
MAX_EVENT_MARKERS_PER_WINDOW = 96

_BLOCK = re.compile(r"\{([^{}]*)\}")
_KARAOKE = re.compile(r"\\(?:kf|ko|k|K)\s*(\d+)", re.IGNORECASE)
_MOVE = re.compile(r"\\move\s*\(([^)]*)\)", re.IGNORECASE)
_FAD = re.compile(r"\\fad\s*\(([^)]*)\)", re.IGNORECASE)
_FADE = re.compile(r"\\fade\s*\(([^)]*)\)", re.IGNORECASE)
_TRANSFORM = re.compile(r"\\t\s*\(([^)]*)\)", re.IGNORECASE)


def _time(value: str) -> float:
    parts = value.strip().split(":")
    if len(parts) != 3:
        raise ValueError("Malformed ASS cue time")
    hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError("ASS cue time outside expected bounds")
    result = hours * 3600 + minutes * 60 + seconds
    if not math.isfinite(result):
        raise ValueError("Nonfinite ASS cue time")
    return result


def _reasons(text: str) -> set[str]:
    result: set[str] = set()
    for block in _BLOCK.findall(text):
        if _KARAOKE.search(block) or re.search(r"\\kt\s*\d+", block, re.IGNORECASE):
            result.add("karaoke")
        if _MOVE.search(block):
            result.add("motion")
        if _FAD.search(block) or _FADE.search(block):
            result.add("fade")
        if _TRANSFORM.search(block):
            result.add("transform")
    return result


def _distributed(items: list[int], count: int) -> list[int]:
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    return [items[round(i * (len(items) - 1) / (count - 1))]
            for i in range(count)]


def _parse_windows(ass_text: str) -> tuple[list[dict[str, Any]], list[tuple[float, float]]]:
    windows: dict[tuple[float, float], dict[str, Any]] = {}
    raw_ranges: list[tuple[float, float]] = []
    in_events = False
    fields: dict[str, int] | None = None
    for line in ass_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.lower() == "[events]"
            fields = None
            continue
        if not in_events:
            continue
        kind, sep, body = stripped.partition(":")
        if not sep:
            continue
        if kind.lower() == "format":
            names = [name.strip().lower() for name in body.split(",")]
            if len(names) != len(set(names)) or not {"start", "end", "text"}.issubset(names):
                raise ValueError("Invalid ASS Events Format fields")
            if names[-1] != "text":
                # ASS requires Text last so its commas can remain unescaped.
                raise ValueError("ASS Events Text must be the final field")
            fields = {name: i for i, name in enumerate(names)}
            continue
        if kind.lower() != "dialogue":
            continue
        if fields is None:
            raise ValueError("ASS Dialogue without Events Format")
        parts = body.strip().split(",", len(fields) - 1)
        if len(parts) != len(fields):
            raise ValueError("Malformed ASS dialogue record")
        start, end = _time(parts[fields["start"]]), _time(parts[fields["end"]])
        if end <= start:
            raise ValueError("Non-positive ASS dialogue duration")
        raw_ranges.append((start, end))
        if len(raw_ranges) > MAX_ASS_DIALOGUES:
            raise ValueError("ASS subtitle cue count exceeds bounded evidence limit")
        key = (start, end)
        if key not in windows:
            windows[key] = {"start": start, "end": end, "texts": [],
                            "dynamic_tags": set()}
        text = parts[fields["text"]]
        windows[key]["texts"].append(text)
        windows[key]["dynamic_tags"].update(_reasons(text))
    if not raw_ranges:
        raise ValueError("ASS has no dialogue events to verify")
    return [windows[k] for k in sorted(windows)], raw_ranges


def _effect_candidates(window: dict[str, Any]) -> tuple[dict[float, set[str]], bool]:
    start, end = window["start"], window["end"]
    duration = end - start
    results: dict[float, set[str]] = {}
    marker_count = 0
    truncated = False

    def add(absolute: float, reason: str) -> None:
        if math.isfinite(absolute) and start <= absolute < end:
            stamp = round(max(start, min(end - 0.001, absolute)), 3)
            results.setdefault(stamp, set()).add(reason)

    def point(offset_ms: float, reason: str) -> None:
        add(start + offset_ms / 1000.0, reason)

    add(start + min(0.02, duration / 4), "animated_entry")
    add(end - min(0.02, duration / 4), "animated_exit")
    for text in window["texts"]:
        for block in _BLOCK.findall(text):
            for match in re.finditer(r"\\(?:kf|ko|k|K)\s*\d+|\\kt\s*\d+|\\(?:move|fad|fade|t)\s*\([^)]*\)", block, re.IGNORECASE):
                if marker_count >= MAX_EVENT_MARKERS_PER_WINDOW:
                    truncated = True
                    break
                tag = match.group()
                karaoke = _KARAOKE.fullmatch(tag)
                if karaoke:
                    # Cumulative karaoke timings are handled in the next pass.
                    continue
                marker_count += 1
                if re.fullmatch(r"\\kt\s*\d+", tag, re.IGNORECASE):
                    # \kt rewrites karaoke start offset. Conservative: sample
                    # entry/mid/exit, but do not claim precise \kt parsing.
                    truncated = True
                    continue
                body = tag[tag.find("(") + 1:-1]
                args = [part.strip() for part in body.split(",")]
                try:
                    if tag.lower().startswith("\\move"):
                        offsets = (float(args[4]), float(args[5])) if len(args) == 6 else (0., duration * 1000)
                        if len(args) not in (4, 6):
                            truncated = True
                            continue
                        for v in (offsets[0], (offsets[0] + offsets[1]) / 2, offsets[1]):
                            point(v, "motion_transition")
                    elif tag.lower().startswith("\\fad"):
                        if len(args) != 2:
                            truncated = True
                            continue
                        fadein, fadeout = float(args[0]), float(args[1])
                        for v in (fadein / 2, fadein, duration * 1000 - fadeout,
                                  duration * 1000 - fadeout / 2):
                            point(v, "fade_transition")
                    elif tag.lower().startswith("\\fade"):
                        if len(args) != 7:
                            truncated = True
                            continue
                        for v in map(float, args[3:]):
                            point(v, "fade_transition")
                    elif tag.lower().startswith("\\t"):
                        if len(args) >= 3 and re.fullmatch(r"-?\d+(?:\.\d+)?", args[0]) and re.fullmatch(r"-?\d+(?:\.\d+)?", args[1]):
                            a, b = float(args[0]), float(args[1])
                        else:
                            a, b = 0., duration * 1000
                        for v in (a, (a + b) / 2, b):
                            point(v, "transform_transition")
                except (ValueError, OverflowError, IndexError):
                    truncated = True
            if marker_count >= MAX_EVENT_MARKERS_PER_WINDOW:
                break
        # Karaoke timeline is decoded separately to ensure cumulative centiseconds
        # span multiple ASS override blocks and remain bounded.
        elapsed_cs = 0
        for block in _BLOCK.findall(text):
            for match in _KARAOKE.finditer(block):
                if marker_count >= MAX_EVENT_MARKERS_PER_WINDOW:
                    truncated = True
                    break
                marker_count += 1
                span_cs = min(int(match.group(1)), 1200000)
                if span_cs <= 0:
                    continue
                point((elapsed_cs + span_cs / 2) * 10, "karaoke_word_mid")
                elapsed_cs += span_cs
                point(elapsed_cs * 10 - 10, "karaoke_pre_change")
                point(elapsed_cs * 10 + 10, "karaoke_post_change")
            if marker_count >= MAX_EVENT_MARKERS_PER_WINDOW:
                break
        if marker_count >= MAX_EVENT_MARKERS_PER_WINDOW:
            break
    return results, truncated


def plan_export_ass_samples(ass_text: str, sample_limit: int = 8) -> dict[str, Any]:
    """Spread limited frames over distinct cues and exported animation events.

    Candidate count comprises one midpoint per unique time window plus decoded
    effect candidates for *targeted* dynamic windows; unsampled windows are not
    expanded, making processing viable for long archives.
    """
    if type(sample_limit) is not int or not 1 <= sample_limit <= MAX_EXPORT_SAMPLES:
        raise ValueError("ASS sample limit must be an integer from 1 to 12")
    windows, raw_ranges = _parse_windows(ass_text)
    n = len(windows)
    dynamic = [i for i, w in enumerate(windows) if w["dynamic_tags"]]
    anchors = _distributed(list(range(n)), min(n, sample_limit) if not dynamic
                           else min(n, max(1, sample_limit // 3)))
    targeted = set(anchors)
    if dynamic:
        # Explicitly reserve budget both for risky windows and within-cue
        # event transitions. Pure midpoint sampling misses those extrema.
        distinct_target = min(len(dynamic), max(1, (sample_limit + 1) // 3))
        for index in _distributed(dynamic, distinct_target):
            if len(targeted) >= sample_limit:
                break
            targeted.add(index)

    reasons_by_time: dict[float, set[str]] = {}
    source_by_time: dict[float, set[int]] = {}

    def register(stamp: float, reasons: set[str], index: int) -> None:
        reasons_by_time.setdefault(stamp, set()).update(reasons)
        source_by_time.setdefault(stamp, set()).add(index)

    for index, w in enumerate(windows):
        stamp = round((w["start"] + w["end"]) / 2, 3)
        register(stamp, {"dialogue_midpoint"}, index)

    effect_candidates: dict[float, set[str]] = {}
    effect_owners: dict[float, set[int]] = {}
    truncated_windows = 0
    for index in sorted(targeted):
        w = windows[index]
        if not w["dynamic_tags"]:
            continue
        extra, truncated = _effect_candidates(w)
        truncated_windows += bool(truncated)
        for stamp, reasons in extra.items():
            effect_candidates.setdefault(stamp, set()).update(reasons)
            effect_owners.setdefault(stamp, set()).add(index)
            register(stamp, reasons, index)

    midpoints = {
        round((w["start"] + w["end"]) / 2, 3) for w in windows
    }
    selected = {round((windows[i]["start"] + windows[i]["end"]) / 2, 3) for i in targeted}
    # Prefer actual motion/fade/Karaoke boundary candidates before consuming
    # remaining budget on more quiet-window midpoint frames.
    while len(selected) < sample_limit:
        options = [t for t in effect_candidates if t not in selected]
        if not options:
            break

        # First target one representative of each distinct animation family;
        # otherwise a long single motion could crowd out all Karaoke events.
        def family(reason: str) -> str:
            return reason.split("_", 1)[0]

        represented = {family(r) for t in selected for r in reasons_by_time[t]}
        def effect_score(stamp: float) -> tuple[float, float, float]:
            owners = effect_owners[stamp]
            nearest = min(
                min((abs(stamp - s) / max(.01, windows[i]["end"] - windows[i]["start"])
                     for s in selected if windows[i]["start"] <= s < windows[i]["end"]),
                    default=1.0)
                for i in owners
            )
            fresh = any(family(r) not in represented and family(r) != "animated"
                        for r in effect_candidates[stamp])
            return float(fresh), nearest, -stamp
        selected.add(max(options, key=effect_score))
    # Fill any remaining capacity with the largest time gap among unvisited
    # Dialogue windows. This is only a deterministic heuristic.
    while len(selected) < min(sample_limit, len(reasons_by_time)):
        choices = [t for t in midpoints if t not in selected]
        if not choices:
            break
        selected.add(max(choices, key=lambda t: (
            min(abs(t - existing) for existing in selected), -t
        )))
    ordered = sorted(selected)
    selected_target_indices: set[int] = set()
    samples = []
    for stamp in ordered:
        target_indices = sorted(source_by_time.get(stamp, set()))
        selected_target_indices.update(target_indices)
        # A rendered FFmpeg frame composites ALL simultaneous Dialogue layers.
        # These are informational counts, never exclusive attribution.
        active_count = sum(start <= stamp < end for start, end in raw_ranges)
        samples.append({
            "timestamp_seconds": stamp,
            "dialogue_start_seconds": min(windows[i]["start"] for i in target_indices),
            "dialogue_end_seconds": max(windows[i]["end"] for i in target_indices),
            "reasons": sorted(reasons_by_time[stamp]),
            "active_dialogue_count": active_count,
            "compositing_scope": "all_active_ass_dialogues",
        })
    candidate_count = len(reasons_by_time)
    covered_dynamic = sum(i in selected_target_indices for i in dynamic)
    return {
        "dialogue_count": len(raw_ranges),
        "unique_window_count": n,
        "unexamined_unique_windows": n - len(selected_target_indices),
        "dynamic_window_count": len(dynamic),
        "targeted_dynamic_window_count": covered_dynamic,
        "unexamined_dynamic_windows": len(dynamic) - covered_dynamic,
        "effect_candidate_window_count": sum(windows[i]["dynamic_tags"] != set() for i in targeted),
        "effect_candidates_truncated_windows": truncated_windows,
        "candidate_count": candidate_count,
        "candidate_count_scope": "window_midpoints_plus_targeted_effect_candidates",
        "omitted_candidate_count": candidate_count - len(selected),
        "max_sample_gap_seconds": round(max((b - a for a, b in zip(ordered, ordered[1:])), default=0.0), 3),
        "sample_limit": sample_limit,
        "sample_count": len(samples),
        "samples": samples,
    }
