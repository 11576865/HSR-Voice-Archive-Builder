"""Non-destructive per-cue subtitle DISPLAY timing overrides.

Original manifest audio positions remain immutable. Timing adjustment is a
derived output-only layer applied to SRT/ASS adapters and project UI rows.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .builder import atomic_write_text

TIMING_OVERRIDES_FILE = "subtitle_timing_overrides.json"
MIN_DURATION_SECONDS = 0.10
MAX_BOUNDARY_ADJUST_SECONDS = 5.0


class StaleSubtitleTimingError(ValueError):
    """Saved display timing refers to a different source clip/time window."""


def _entry_id(entry: dict[str, Any]) -> str:
    value = entry.get("index")
    if value is None:
        value = entry.get("id") or entry.get("filename")
    return str(value) if value is not None else ""


def source_subtitle_window(entry: dict[str, Any]) -> tuple[float, float]:
    return (
        float(entry.get("start_seconds", 0.0)),
        float(entry.get("display_end_seconds", entry.get("audio_end_seconds", 0.0))),
    )


def read_timing_overrides(output_dir: Path) -> dict[str, dict[str, float]]:
    path = output_dir / TIMING_OVERRIDES_FILE
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("Unsupported subtitle display timing overrides schema")
    records = raw.get("entries")
    if not isinstance(records, dict):
        raise ValueError("Subtitle timing overrides entries must be a mapping")
    parsed = {}
    for key, value in records.items():
        if not isinstance(value, dict) or not isinstance(key, str):
            raise ValueError("Invalid subtitle timing override entry")
        start = _finite_seconds(value.get("start"), "start")
        end = _finite_seconds(value.get("end"), "end")
        if start < 0 or end-start < MIN_DURATION_SECONDS:
            raise ValueError("Invalid stored subtitle timing window")
        parsed[key] = {"start": start, "end": end}
        if "source_start" in value and "source_end" in value:
            parsed[key]["source_start"] = _finite_seconds(value["source_start"], "source_start")
            parsed[key]["source_end"] = _finite_seconds(value["source_end"], "source_end")
            parsed[key]["source_member_id"] = str(value.get("source_member_id") or "")
    return parsed


def _finite_seconds(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Subtitle {label} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Subtitle {label} must be a finite number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"Subtitle {label} must be a finite number")
    return round(parsed, 3)


def _validate_window(start: float, end: float, source_start: float, source_end: float) -> None:
    if start < 0 or end-start < MIN_DURATION_SECONDS:
        raise ValueError("Subtitle end must follow start by at least 0.10 seconds")
    if (
        abs(start-source_start) > MAX_BOUNDARY_ADJUST_SECONDS
        or abs(end-source_end) > MAX_BOUNDARY_ADJUST_SECONDS
    ):
        raise ValueError("Subtitle display adjustment must stay within 5 seconds of each source boundary")


def _effective_window(entry: dict[str, Any], overrides: dict[str, dict[str, float]]) -> tuple[float, float]:
    source_start, source_end = source_subtitle_window(entry)
    override = overrides.get(_entry_id(entry))
    if not override:
        return source_start, source_end
    if "source_start" in override and "source_end" in override:
        if (
            abs(override["source_start"] - source_start) > 0.005
            or abs(override["source_end"] - source_end) > 0.005
            or str(override.get("source_member_id") or "") != str(entry.get("source_member_id") or "")
        ):
            raise StaleSubtitleTimingError("Subtitle timing override targets an outdated audio timeline; review it before exporting")
    _validate_window(override["start"], override["end"], source_start, source_end)
    return override["start"], override["end"]


def effective_subtitle_window(
    entry: dict[str, Any], overrides: dict[str, dict[str, float]]
) -> tuple[float, float]:
    return _effective_window(entry, overrides)


def update_subtitle_display_timing(
    output_dir: Path,
    *,
    item_id: str | int,
    start: Any = None,
    end: Any = None,
    reset: bool = False,
    expected_start: Any = None,
    expected_end: Any = None,
) -> dict[str, Any]:
    """Validate and write one output-only timing change.

    Optimistic-concurrency expected_start/end refers to the *currently
    effective* subtitle display window, not raw audio positions.
    """
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("Build the archive before adjusting subtitle display timing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Manifest entries must be a list")
    key = str(item_id)
    matches = [entry for entry in entries if isinstance(entry, dict) and _entry_id(entry) == key]
    if len(matches) != 1:
        raise ValueError("Subtitle id is missing or ambiguous in the archive")
    entry = matches[0]
    raw_start, raw_end = source_subtitle_window(entry)
    overrides = read_timing_overrides(output_dir)
    try:
        previous_start, previous_end = _effective_window(entry, overrides)
    except StaleSubtitleTimingError:
        if not reset:
            raise
        # A stale derived overlay must be recoverable without editing raw audio.
        # The operator must explicitly reset, acknowledging the current source.
        previous_start, previous_end = raw_start, raw_end
    if expected_start is None or expected_end is None:
        raise ValueError("Expected current subtitle display start/end are required")
    expected_pair = (_finite_seconds(expected_start, "expected_start"), _finite_seconds(expected_end, "expected_end"))
    if expected_pair != (round(previous_start, 3), round(previous_end, 3)):
        raise ValueError("Subtitle display timing changed since it was loaded; refresh before editing")
    if reset:
        new_start,new_end = raw_start,raw_end
        overrides.pop(key, None)
    else:
        new_start = _finite_seconds(start, "start")
        new_end = _finite_seconds(end, "end")
        _validate_window(new_start, new_end, raw_start, raw_end)
        if (new_start, new_end) == (raw_start, raw_end):
            overrides.pop(key, None)
        else:
            overrides[key] = {
                "start": new_start,
                "end": new_end,
                "source_start": round(raw_start, 3),
                "source_end": round(raw_end, 3),
                "source_member_id": str(entry.get("source_member_id") or ""),
            }
    path = output_dir / TIMING_OVERRIDES_FILE
    atomic_write_text(
        path,
        json.dumps({"schema_version": 1, "entries": overrides}, ensure_ascii=False, indent=2),
    )
    return {
        "id": key,
        "start": new_start,
        "end": new_end,
        "source_start": raw_start,
        "source_end": raw_end,
        "timing_modified": key in overrides,
        "override_count": len(overrides),
    }
