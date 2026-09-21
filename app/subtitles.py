from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .builder import atomic_write_text, write_csv_rows
from .project import ProjectConfig
from .timeline import write_ass, write_srt


@dataclass
class SubtitleItem:
    id: int | str
    start: float
    end: float
    source_language: str
    source_text: str
    official_chs: str
    api_chs: str
    final_chs: str
    modified: bool


class SubtitleEntryAdapter:
    """Lightweight adapter to pass entries to write_ass and write_srt."""

    def __init__(
        self,
        english: str,
        chinese: str,
        start_seconds: float,
        display_end_seconds: float,
    ):
        self.english = english
        self.chinese = chinese
        self.start_seconds = start_seconds
        self.display_end_seconds = display_end_seconds


def parse_time_range_str(time_range_str: str) -> tuple[float | None, float | None]:
    """Parse 'MM:SS - MM:SS' string into (start_seconds, end_seconds)."""
    if not time_range_str or not time_range_str.strip():
        return None, None

    raw = time_range_str.strip()
    parts = [p.strip() for p in raw.split("-")]
    if len(parts) != 2:
        return None, None

    def time_to_seconds(part: str) -> float | None:
        if not part:
            return None
        subparts = part.split(":")
        if len(subparts) == 2:
            try:
                m = float(subparts[0])
                s = float(subparts[1])
                return m * 60.0 + s
            except ValueError:
                return None
        elif len(subparts) == 3:
            try:
                h = float(subparts[0])
                m = float(subparts[1])
                s = float(subparts[2])
                return h * 3600.0 + m * 60.0 + s
            except ValueError:
                return None
        else:
            try:
                return float(part)
            except ValueError:
                return None

    s_time = time_to_seconds(parts[0])
    e_time = time_to_seconds(parts[1])
    return s_time, e_time


def get_project_subtitles(
    config: ProjectConfig,
    output_dir: Path | None,
    q: str | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
    selector: str | None = None,
) -> list[dict[str, Any]]:
    source_lang = config.source_text_language or "en"
    ref_lang = config.reference_language or "auto"
    manifest_file = output_dir / "manifest.json" if output_dir else None

    if not manifest_file or not manifest_file.is_file():
        # Fallback mock data when output manifest is not yet built
        subtitles = [
            {
                "id": 1,
                "start": 5.0,
                "end": 7.5,
                "source_language": source_lang,
                "source_text": "May this journey lead us starward.",
                "official_chs": "愿此行，终抵群星。",
                "api_chs": "愿这场旅程带我们走向群星。",
                "final_chs": "愿此行，终抵群星。",
                "modified": False,
            },
            {
                "id": 2,
                "start": 8.0,
                "end": 11.2,
                "source_language": source_lang,
                "source_text": "Rules are made to be broken!",
                "official_chs": "规则，就是用来打破的！",
                "api_chs": "规矩就是用来打破的！",
                "final_chs": "规则，就是用来打破的！",
                "modified": False,
            },
        ]
        if q and q.strip():
            query = q.strip().casefold()
            subtitles = [
                sub for sub in subtitles
                if query in sub["source_text"].casefold()
                or query in sub["official_chs"].casefold()
                or query in sub["api_chs"].casefold()
                or query in sub["final_chs"].casefold()
            ]
        if start_time is not None:
            subtitles = [sub for sub in subtitles if sub["end"] >= start_time]
        if end_time is not None:
            subtitles = [sub for sub in subtitles if sub["start"] <= end_time]
        return subtitles

    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception:
        return []

    entries = data.get("entries", [])
    overrides_file = output_dir / "subtitles_overrides.json"
    overrides: dict[str, dict[str, Any]] = {}
    if overrides_file.is_file():
        try:
            overrides = json.loads(overrides_file.read_text(encoding="utf-8"))
        except Exception:
            overrides = {}

    subtitles: list[dict[str, Any]] = []
    source_lang = config.source_text_language or "en"
    ref_lang = config.reference_language or "auto"

    for entry in entries:
        item_id = entry.get("index")
        if item_id is None:
            item_id = entry.get("id") or entry.get("filename")

        src_type = str(entry.get("target_text_source") or entry.get("chinese_source", ""))
        target_text = str(entry.get("target_text") or entry.get("chinese", ""))

        official_chs = ""
        api_chs = ""

        if src_type in ("official_chs_lab", "official_target_lab"):
            official_chs = target_text
        elif src_type.startswith("api:") or src_type in ("translated_existing", "api_translation"):
            api_chs = target_text
            ref_text = str(entry.get("reference_text", ""))
            entry_ref_lang = str(entry.get("reference_language", ""))
            if entry_ref_lang in ("zh-CN", "zh") or ref_lang in ("zh-CN", "zh"):
                official_chs = ref_text
        else:
            official_chs = target_text

        final_chs = target_text
        modified = bool(entry.get("modified", False))

        str_id = str(item_id)
        if str_id in overrides:
            ov = overrides[str_id]
            if isinstance(ov, dict):
                final_chs = str(ov.get("final_chs", final_chs))
                modified = bool(ov.get("modified", True))

        start = float(entry.get("start_seconds", 0.0))
        end = float(entry.get("display_end_seconds", entry.get("audio_end_seconds", 0.0)))

        item = {
            "id": item_id,
            "start": start,
            "end": end,
            "source_language": source_lang,
            "source_text": str(entry.get("source_text") or entry.get("english", "")),
            "official_chs": official_chs,
            "api_chs": api_chs,
            "final_chs": final_chs,
            "modified": modified,
        }
        subtitles.append(item)

    # Filtering by selector
    if selector:
        sel = selector.strip().lower()
        if sel in ("modified", "modified_subtitles"):
            subtitles = [s for s in subtitles if s["modified"]]
        elif sel in ("official", "official_chinese", "official_chs"):
            subtitles = [s for s in subtitles if bool(s["official_chs"])]
        elif sel in ("api", "api_translated", "api_chs"):
            subtitles = [s for s in subtitles if bool(s["api_chs"])]

    # Filtering by search query (q)
    if q and q.strip():
        query = q.strip().casefold()
        subtitles = [
            sub
            for sub in subtitles
            if query in sub["source_text"].casefold()
            or query in sub["official_chs"].casefold()
            or query in sub["api_chs"].casefold()
            or query in sub["final_chs"].casefold()
            or query in str(sub["id"]).casefold()
        ]

    # Filtering by time range
    if start_time is not None:
        subtitles = [sub for sub in subtitles if sub["end"] >= start_time]
    if end_time is not None:
        subtitles = [sub for sub in subtitles if sub["start"] <= end_time]

    return subtitles


def update_project_subtitles(
    config: ProjectConfig,
    output_dir: Path,
    updates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Save edited final_chs text, update overrides and manifest files, and regenerate ASS and SRT files."""

    manifest_file = output_dir / "manifest.json"
    if not manifest_file.is_file():
        raise FileNotFoundError("Manifest file not found in project output directory")

    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    entries = data.get("entries", [])

    overrides_file = output_dir / "subtitles_overrides.json"
    overrides: dict[str, dict[str, Any]] = {}
    if overrides_file.is_file():
        try:
            overrides = json.loads(overrides_file.read_text(encoding="utf-8"))
        except Exception:
            overrides = {}

    updates_by_id: dict[str, str] = {}
    for item in updates:
        item_id = item.get("id")
        if item_id is not None and "final_chs" in item:
            updates_by_id[str(item_id)] = str(item["final_chs"])

    updated_count = 0
    for entry in entries:
        item_id = entry.get("index")
        if item_id is None:
            item_id = entry.get("id") or entry.get("filename")

        str_id = str(item_id)
        if str_id in updates_by_id:
            new_chs = updates_by_id[str_id]
            # Only final_chs is edited. Do NOT modify timestamps, source_text, official_chs, api_chs.
            entry["target_text"] = new_chs
            entry["chinese"] = new_chs
            entry["modified"] = True
            overrides[str_id] = {
                "final_chs": new_chs,
                "modified": True,
            }
            updated_count += 1

    # Save subtitles_overrides.json
    atomic_write_text(
        overrides_file,
        json.dumps(overrides, ensure_ascii=False, indent=2),
    )

    # Save manifest.json
    atomic_write_text(
        manifest_file,
        json.dumps(data, ensure_ascii=False, indent=2),
    )

    # Update manifest.csv if present
    manifest_csv = output_dir / "manifest.csv"
    if manifest_csv.is_file() and entries:
        fields = list(entries[0].keys())
        write_csv_rows(manifest_csv, entries, fields)

    # Update bilingual_index_corrected.csv if present
    corrected_csv = output_dir / "bilingual_index_corrected.csv"
    if corrected_csv.is_file():
        try:
            with corrected_csv.open("r", encoding="utf-8-sig", newline="") as f:
                old_rows = list(dict(row) for row in json.loads(json.dumps(list(json.JSONDecoder().decode("{}")))) if False)
        except Exception:
            pass

    # Reconstruct subtitle adapter entries for ASS/SRT export
    adapter_entries: list[SubtitleEntryAdapter] = []
    for entry in entries:
        english = str(entry.get("source_text") or entry.get("english", ""))
        chinese = str(entry.get("target_text") or entry.get("chinese", ""))
        start_sec = float(entry.get("start_seconds", 0.0))
        end_sec = float(entry.get("display_end_seconds", entry.get("audio_end_seconds", 0.0)))
        adapter_entries.append(
            SubtitleEntryAdapter(
                english=english,
                chinese=chinese,
                start_seconds=start_sec,
                display_end_seconds=end_sec,
            )
        )

    src_lang = config.source_text_language or "en"
    target_lang = config.target_language or "zh-CN"

    # Regenerate ASS & SRT files and overwrite existing ones
    ass_file = output_dir / "HSR_Voice_Archive.ass"
    srt_file = output_dir / "HSR_Voice_Archive.srt"

    write_ass(adapter_entries, ass_file, source_language=src_lang, target_language=target_lang)
    write_srt(adapter_entries, srt_file, source_language=src_lang, target_language=target_lang)

    return {
        "ok": True,
        "updated_count": updated_count,
        "total_count": len(entries),
        "ass_file": str(ass_file),
        "srt_file": str(srt_file),
    }
