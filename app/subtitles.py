from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .builder import atomic_write_text, write_csv_rows
from .project import ProjectConfig, resolve_project_path
from .timeline import write_ass, write_srt
from subtitle_layout.config import SubtitleRenderConfig
from subtitle_layout.fonts import validate_ass_font_name


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
    """Lightweight adapter to pass effective final text to subtitle writers."""

    def __init__(
        self,
        item_id: int | str,
        english: str,
        chinese: str,
        start_seconds: float,
        display_end_seconds: float,
        word_alignments: list[object] | None = None,
    ):
        self.id = item_id
        self.english = english
        self.chinese = chinese
        self.start_seconds = start_seconds
        self.display_end_seconds = display_end_seconds
        self.word_alignments = word_alignments


def _entry_id(entry: dict[str, Any]) -> int | str | None:
    item_id = entry.get("index")
    if item_id is None:
        item_id = entry.get("id") or entry.get("filename")
    return item_id


def load_subtitle_review_state(output_dir: Path) -> dict[str, bool]:
    path = output_dir / "subtitle_review_state.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): bool(value) for key, value in payload.items()}


def _write_subtitle_review_state(output_dir: Path, state: dict[str, bool]) -> None:
    atomic_write_text(
        output_dir / "subtitle_review_state.json",
        json.dumps(
            {key: True for key, value in state.items() if value},
            ensure_ascii=False,
            indent=2,
        ),
    )


def load_subtitle_overrides(output_dir: Path) -> dict[str, dict[str, Any]]:
    path = output_dir / "subtitles_overrides.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): value
        for key, value in payload.items()
        if isinstance(value, dict)
    }


def _override_for_entry(
    entry: dict[str, Any],
    overrides: dict[str, dict[str, Any]],
    ambiguous_filenames: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    item_id = _entry_id(entry)
    filename = str(entry.get("filename", "") or "")
    logical_id = str(entry.get("logical_id", "") or "")
    member_id = str(entry.get("source_member_id", "") or "")

    if item_id is not None:
        exact = overrides.get(str(item_id))
        if isinstance(exact, dict):
            stored_filename = str(exact.get("filename", "") or "")
            stored_logical_id = str(exact.get("logical_id", "") or "")
            stored_member = str(exact.get("source_member_id", "") or "")
            if (
                (not stored_filename or not filename or stored_filename == filename)
                and (not stored_logical_id or not logical_id or stored_logical_id == logical_id)
                and (not stored_member or not member_id or stored_member == member_id)
            ):
                return exact

    for candidate in overrides.values():
        stored_filename = str(candidate.get("filename", "") or "")
        stored_logical_id = str(candidate.get("logical_id", "") or "")
        stored_member = str(candidate.get("source_member_id", "") or "")
        if filename and stored_filename == filename:
            # Same-basename entries make a bare filename match ambiguous:
            # only trust it when the member ids agree, or when neither side
            # is member-aware and the filename is unique in this project.
            if stored_member or member_id:
                if stored_member and member_id and stored_member == member_id:
                    return candidate
                continue
            if filename in ambiguous_filenames:
                continue
            return candidate
        if logical_id and stored_logical_id == logical_id:
            if filename in ambiguous_filenames:
                continue
            return candidate
    return None


def _apply_derived_subtitle_fields(
    entries: list[dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
) -> None:
    filename_counts: dict[str, int] = {}
    for entry in entries:
        name = str(entry.get("filename", "") or "")
        if name:
            filename_counts[name] = filename_counts.get(name, 0) + 1
    ambiguous = frozenset(name for name, count in filename_counts.items() if count > 1)
    for entry in entries:
        raw_target = str(entry.get("target_text") or entry.get("chinese", ""))
        override = _override_for_entry(entry, overrides, ambiguous)
        if override is None:
            entry["final_chs"] = raw_target
            entry["modified"] = False
            continue
        entry["final_chs"] = str(override.get("final_chs", raw_target))
        entry["modified"] = bool(override.get("modified", True))


def _sync_corrected_csv(
    output_dir: Path,
    entries: list[dict[str, Any]],
) -> None:
    corrected = output_dir / "bilingual_index_corrected.csv"
    if not corrected.is_file():
        return
    try:
        with corrected.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
            fields = list(reader.fieldnames or [])
    except (OSError, csv.Error):
        return

    by_member = {
        str(entry.get("source_member_id", "")): entry
        for entry in entries
        if str(entry.get("source_member_id", "") or "")
    }
    by_filename = {
        str(entry.get("filename", "")): entry
        for entry in entries
        if str(entry.get("filename", ""))
    }
    by_index = {
        str(_entry_id(entry)): entry
        for entry in entries
        if _entry_id(entry) is not None
    }
    for row in rows:
        entry = by_member.get(str(row.get("source_member_id", "") or ""))
        if entry is None:
            entry = by_filename.get(str(row.get("filename", "")))
        if entry is None:
            entry = by_index.get(str(row.get("index", "")))
        if entry is None:
            continue
        row["final_chs"] = str(entry.get("final_chs", ""))
        row["modified"] = "true" if bool(entry.get("modified", False)) else "false"

    for field in ("final_chs", "modified"):
        if field not in fields:
            fields.append(field)
    write_csv_rows(corrected, rows, fields)


def _subtitle_adapters(entries: list[dict[str, Any]]) -> list[SubtitleEntryAdapter]:
    adapters: list[SubtitleEntryAdapter] = []
    for entry in entries:
        adapters.append(
            SubtitleEntryAdapter(
                item_id=_entry_id(entry) or "",
                english=str(entry.get("source_text") or entry.get("english", "")),
                chinese=str(
                    entry.get("final_chs")
                    or entry.get("target_text")
                    or entry.get("chinese", "")
                ),
                start_seconds=float(entry.get("start_seconds", 0.0)),
                display_end_seconds=float(
                    entry.get(
                        "display_end_seconds",
                        entry.get("audio_end_seconds", 0.0),
                    )
                ),
                word_alignments=(
                    entry.get("word_alignments")
                    or entry.get("words")
                    or None
                ),
            )
        )
    return adapters


def subtitle_render_config(config: ProjectConfig) -> SubtitleRenderConfig:
    """Build the single render configuration used by ASS export."""
    return SubtitleRenderConfig(
        enable_karaoke=bool(config.subtitle_enable_karaoke),
        enable_frosted_glass=bool(config.subtitle_enable_translucent_card),
        enable_multi_layer_outline=bool(config.subtitle_enable_multi_layer_outline),
        enable_kinetic=bool(config.subtitle_enable_kinetic),
        fade_in_ms=max(0, min(2000, int(config.subtitle_fade_in_ms))),
        fade_out_ms=max(0, min(2000, int(config.subtitle_fade_out_ms))),
        use_audio_aware_fade=bool(config.subtitle_use_audio_aware_fade),
        outline_width=max(0.0, min(12.0, float(config.subtitle_outline_width))),
        shadow_depth=max(0.0, min(12.0, float(config.subtitle_shadow_depth))),
        blur_radius=max(0.0, min(5.0, float(config.subtitle_blur_radius))),
        card_opacity=max(0.0, min(1.0, float(config.subtitle_card_opacity))),
        enable_soft_entry=bool(config.subtitle_enable_soft_entry),
        soft_entry_scale_percent=max(90.0, min(100.0, float(config.subtitle_soft_entry_scale_percent))),
        soft_entry_blur=max(0.0, min(5.0, float(config.subtitle_soft_entry_blur))),
        soft_entry_ms=max(0, min(1000, int(config.subtitle_soft_entry_ms))),
        chs_font=validate_ass_font_name(config.subtitle_chs_font or "汉仪旗黑"),
        primary_font=validate_ass_font_name(config.subtitle_primary_font or "Noto Sans"),
        base_chs_size=max(12, min(120, int(config.subtitle_chs_size))),
        base_primary_size=max(12, min(120, int(config.subtitle_primary_size))),
        margin_horizontal_percent=max(
            0.0, min(0.40, float(config.subtitle_margin_horizontal_percent))
        ),
        margin_vertical_percent=max(
            0.0, min(0.40, float(config.subtitle_margin_vertical_percent))
        ),
        min_central_gap=max(0.0, min(200.0, float(config.subtitle_min_central_gap))),
    )


def invalidate_subtitle_stages(config: ProjectConfig) -> None:
    """Invalidate only stages whose artifacts contain subtitle text."""

    state_dir = resolve_project_path(config, config.state_dir)
    if state_dir is None:
        return
    for relative in ("stages/05_manifest.json", "stages/final_report.json"):
        (state_dir / relative).unlink(missing_ok=True)


def refresh_subtitle_artifacts_from_settings(
    output_dir: Path,
    *,
    source_language: str = "en",
    target_language: str = "zh-CN",
    generate_ass: bool = False,
    render_config: SubtitleRenderConfig | None = None,
    manifest_data: dict[str, Any] | None = None,
    strict_ass: bool = True,
) -> dict[str, Any]:
    """Apply human overrides as a derived layer without mutating source provenance."""

    manifest_file = output_dir / "manifest.json"
    if manifest_data is None:
        if not manifest_file.is_file():
            raise FileNotFoundError("Manifest file not found in project output directory")
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))

    entries = manifest_data.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("Manifest entries must be a list")

    overrides = load_subtitle_overrides(output_dir)
    _apply_derived_subtitle_fields(entries, overrides)

    atomic_write_text(
        manifest_file,
        json.dumps(manifest_data, ensure_ascii=False, indent=2),
    )
    if entries:
        write_csv_rows(output_dir / "manifest.csv", entries, list(entries[0].keys()))
    _sync_corrected_csv(output_dir, entries)

    adapters = _subtitle_adapters(entries)
    source_language = source_language or "en"
    target_language = target_language or "zh-CN"
    ass_file = output_dir / "HSR_Voice_Archive.ass"
    srt_file = output_dir / "HSR_Voice_Archive.srt"
    overflow_file = output_dir / "ass_layout_overflow_report.json"

    # SRT is independent from ASS layout validation and must always reflect the
    # saved human text, even when the richer ASS representation is rejected.
    write_srt(
        adapters,
        srt_file,
        source_language=source_language,
        target_language=target_language,
    )

    ass_error = ""
    if generate_ass:
        try:
            write_ass(
                adapters,
                ass_file,
                source_language=source_language,
                target_language=target_language,
                config=render_config,
            )
        except Exception as exc:
            ass_file.unlink(missing_ok=True)
            ass_error = f"{type(exc).__name__}: {exc}"
            if strict_ass:
                raise
    else:
        ass_file.unlink(missing_ok=True)
        overflow_file.unlink(missing_ok=True)

    return {
        "ass_file": str(ass_file) if generate_ass and not ass_error else "",
        "ass_error": ass_error,
        "srt_file": str(srt_file),
        "override_count": sum(bool(entry.get("modified")) for entry in entries),
    }


def refresh_subtitle_artifacts(
    config: ProjectConfig,
    output_dir: Path,
    manifest_data: dict[str, Any] | None = None,
    *,
    strict_ass: bool = True,
) -> dict[str, Any]:
    return refresh_subtitle_artifacts_from_settings(
        output_dir,
        source_language=config.source_text_language or "en",
        target_language=config.target_language or "zh-CN",
        # ASS is now an on-demand finished output. If the user has already
        # generated it, keep it synchronized with later human proofreading.
        generate_ass=(
            (output_dir / "HSR_Voice_Archive.ass").is_file()
            or bool(getattr(config, "generate_ass", False))
        ),
        render_config=subtitle_render_config(config),
        manifest_data=manifest_data,
        strict_ass=strict_ass,
    )


def parse_time_range_str(time_range_str: str) -> tuple[float | None, float | None]:
    """Parse 'MM:SS - MM:SS' string into (start_seconds, end_seconds)."""
    if not time_range_str or not time_range_str.strip():
        return None, None

    raw = time_range_str.strip()
    for sep in ("--", "—", "–", "~"):
        raw = raw.replace(sep, "-")

    parts = [p.strip() for p in raw.split("-") if p.strip()]
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
                "original_chs": "愿此行，终抵群星。",
                "final_chs": "愿此行，终抵群星。",
                "modified": False,
                "confirmed": False,
            },
            {
                "id": 2,
                "start": 8.0,
                "end": 11.2,
                "source_language": source_lang,
                "source_text": "Rules are made to be broken!",
                "official_chs": "规则，就是用来打破的！",
                "api_chs": "规矩就是用来打破的！",
                "original_chs": "规则，就是用来打破的！",
                "final_chs": "规则，就是用来打破的！",
                "modified": False,
                "confirmed": False,
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
    review_state = load_subtitle_review_state(output_dir)
    if overrides_file.is_file():
        try:
            overrides = json.loads(overrides_file.read_text(encoding="utf-8"))
        except Exception:
            overrides = {}

    overflow_file = output_dir / "ass_layout_overflow_report.json"
    overflow_map: dict[str, dict[str, Any]] = {}
    if overflow_file.is_file():
        try:
            reports = json.loads(overflow_file.read_text(encoding="utf-8"))
            if isinstance(reports, list):
                for rep in reports:
                    if isinstance(rep, dict) and "subtitle_id" in rep:
                        overflow_map[str(rep["subtitle_id"])] = rep
        except Exception:
            overflow_map = {}

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

        final_chs = str(entry.get("final_chs", target_text))
        modified = bool(entry.get("modified", False))

        str_id = str(item_id)
        if str_id in overrides:
            ov = overrides[str_id]
            if isinstance(ov, dict):
                final_chs = str(ov.get("final_chs", final_chs))
                modified = bool(ov.get("modified", True))

        start = float(entry.get("start_seconds", 0.0))
        end = float(entry.get("display_end_seconds", entry.get("audio_end_seconds", 0.0)))

        overflow_info = overflow_map.get(str_id)
        item = {
            "id": item_id,
            "filename": str(entry.get("filename", "") or ""),
            "source_member_id": str(entry.get("source_member_id", "") or ""),
            "logical_id": str(entry.get("logical_id", "") or ""),
            "start": start,
            "end": end,
            "source_language": source_lang,
            "source_text": str(entry.get("source_text") or entry.get("english", "")),
            "official_chs": official_chs,
            "api_chs": api_chs,
            "original_chs": target_text,
            "final_chs": final_chs,
            "modified": modified,
            "confirmed": bool(review_state.get(str_id, False)),
            "layout_overflow": overflow_info is not None,
            "overflow_condition": overflow_info.get("failed_condition") if overflow_info else None,
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
        elif sel in ("overflow", "layout_overflow"):
            subtitles = [s for s in subtitles if s.get("layout_overflow")]
        elif sel in ("unreviewed", "needs_review"):
            subtitles = [s for s in subtitles if not s.get("confirmed")]
        elif sel in ("confirmed", "reviewed"):
            subtitles = [s for s in subtitles if s.get("confirmed")]

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
    """Persist final_chs as a non-destructive override and refresh subtitle artifacts."""

    manifest_file = output_dir / "manifest.json"
    if not manifest_file.is_file():
        raise FileNotFoundError("Manifest file not found in project output directory")

    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("Manifest entries must be a list")

    overrides = load_subtitle_overrides(output_dir)
    review_state = load_subtitle_review_state(output_dir)
    if not updates:
        raise ValueError("No subtitle updates supplied")
    updates_by_id = {
        str(item.get("id")): str(item["final_chs"])
        for item in updates
        if item.get("id") is not None and "final_chs" in item
    }
    confirmations = {
        str(item.get("id")): bool(item.get("confirmed"))
        for item in updates
        if item.get("id") is not None and "confirmed" in item
    }

    updated_count = 0
    text_updated_count = 0
    for entry in entries:
        item_id = _entry_id(entry)
        if item_id is None or str(item_id) not in updates_by_id:
            continue

        key = str(item_id)
        new_chs = updates_by_id[key]
        raw_target = str(entry.get("target_text") or entry.get("chinese", ""))
        filename = str(entry.get("filename", "") or "")
        logical_id = str(entry.get("logical_id", "") or "")
        member_id = str(entry.get("source_member_id", "") or "")

        if new_chs == raw_target:
            overrides.pop(key, None)
            for old_key, value in list(overrides.items()):
                stored_member = str(value.get("source_member_id", "") or "")
                if member_id or stored_member:
                    # Member-aware records only match by member id so a
                    # same-basename sibling never loses its override.
                    if member_id and stored_member and member_id == stored_member:
                        overrides.pop(old_key, None)
                    continue
                if (
                    (filename and str(value.get("filename", "") or "") == filename)
                    or (
                        logical_id
                        and str(value.get("logical_id", "") or "") == logical_id
                    )
                ):
                    overrides.pop(old_key, None)
        else:
            overrides[key] = {
                "final_chs": new_chs,
                "modified": True,
                "filename": filename,
                "logical_id": logical_id,
                "source_member_id": member_id,
            }
        if key in confirmations:
            if confirmations[key]:
                review_state[key] = True
            else:
                review_state.pop(key, None)
        updated_count += 1
        text_updated_count += 1

    # Confirmation-only updates are valid even when the final text is unchanged.
    entry_ids = {str(_entry_id(entry)) for entry in entries if _entry_id(entry) is not None}
    for key, confirmed in confirmations.items():
        if key not in entry_ids or key in updates_by_id:
            continue
        if confirmed:
            review_state[key] = True
        else:
            review_state.pop(key, None)
        updated_count += 1

    atomic_write_text(
        output_dir / "subtitles_overrides.json",
        json.dumps(overrides, ensure_ascii=False, indent=2),
    )

    _write_subtitle_review_state(output_dir, review_state)

    if text_updated_count:
        # A text edit changes manifest/subtitle artifacts, but it must not
        # invalidate or rebuild the already verified continuous FLAC stage.
        invalidate_subtitle_stages(config)
        refreshed = refresh_subtitle_artifacts(
            config,
            output_dir,
            data,
            strict_ass=False,
        )
    else:
        refreshed = {
            "ass_file": str(output_dir / "HSR_Voice_Archive.ass")
            if (output_dir / "HSR_Voice_Archive.ass").is_file()
            else "",
            "ass_error": "",
            "srt_file": str(output_dir / "HSR_Voice_Archive.srt")
            if (output_dir / "HSR_Voice_Archive.srt").is_file()
            else "",
            "override_count": len(overrides),
        }

    if updated_count < 1:
        raise ValueError("No matching subtitle entries were updated")

    return {
        "ok": True,
        "updated_count": updated_count,
        "updated_ids": sorted(updates_by_id),
        "modified_ids": sorted(str(key) for key in overrides),
        "confirmed_ids": sorted(str(key) for key, value in review_state.items() if value),
        "total_count": len(entries),
        **refreshed,
    }

