from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .builder import atomic_write_text

SCHEMA_VERSION = 1
CACHE_FILENAME = "word_alignments.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entry_id(entry: dict[str, Any]) -> str:
    value = entry.get("index")
    if value is None:
        value = entry.get("id") or entry.get("filename") or ""
    return str(value)


def _source_text(entry: dict[str, Any]) -> str:
    return str(entry.get("source_text") or entry.get("english") or "")


def _normalized_text(value: str) -> str:
    return "".join(str(value or "").split())


def _source_fingerprint(text: str) -> str:
    return hashlib.sha256(_normalized_text(text).encode("utf-8")).hexdigest()


def _entry_duration(entry: dict[str, Any]) -> float:
    for key in ("source_duration_seconds", "audio_duration_seconds"):
        try:
            value = float(entry.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value

    try:
        start = float(entry.get("start_seconds", 0.0) or 0.0)
        audio_end = float(entry.get("audio_end_seconds", 0.0) or 0.0)
        if audio_end > start:
            return audio_end - start
        display_end = float(entry.get("display_end_seconds", 0.0) or 0.0)
        if display_end > start:
            return display_end - start
    except (TypeError, ValueError):
        pass
    return 0.0


def alignment_key(entry: dict[str, Any]) -> str:
    member = str(entry.get("source_member_id") or "").replace("\\", "/").strip()
    if member:
        return f"member:{member}"
    logical = str(entry.get("logical_id") or "").strip()
    if logical:
        return f"logical:{logical}"
    return f"id:{_entry_id(entry)}"


def _sanitize_words(words: Any) -> list[dict[str, Any]]:
    if not isinstance(words, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in words:
        if not isinstance(raw, dict):
            return []
        try:
            word = str(raw.get("word", ""))
            start = float(raw.get("start", 0.0))
            end = float(raw.get("end", 0.0))
        except (TypeError, ValueError):
            return []
        item: dict[str, Any] = {"word": word, "start": start, "end": end}
        if raw.get("confidence") is not None:
            try:
                item["confidence"] = float(raw["confidence"])
            except (TypeError, ValueError):
                pass
        result.append(item)
    return result


def validate_word_alignment(
    text: str,
    duration_seconds: float,
    words: Any,
) -> dict[str, Any]:
    expected = _normalized_text(text)
    sanitized = _sanitize_words(words)
    if not expected:
        return {
            "available": False,
            "valid": False,
            "reason": "source_text_empty",
            "word_count": 0,
            "coverage_percent": 0.0,
            "words": [],
        }
    if not sanitized:
        return {
            "available": False,
            "valid": False,
            "reason": "missing",
            "word_count": 0,
            "coverage_percent": 0.0,
            "words": [],
        }

    covered = ""
    previous_end = 0.0
    for item in sanitized:
        word = item["word"]
        start = item["start"]
        end = item["end"]
        if not word:
            return {
                "available": True,
                "valid": False,
                "reason": "empty_word",
                "word_count": len(sanitized),
                "coverage_percent": 0.0,
                "words": sanitized,
            }
        if not math.isfinite(start) or not math.isfinite(end):
            return {
                "available": True,
                "valid": False,
                "reason": "non_finite_time",
                "word_count": len(sanitized),
                "coverage_percent": 0.0,
                "words": sanitized,
            }
        if start < -0.001 or end <= start:
            return {
                "available": True,
                "valid": False,
                "reason": "invalid_time_range",
                "word_count": len(sanitized),
                "coverage_percent": 0.0,
                "words": sanitized,
            }
        if start + 0.02 < previous_end:
            return {
                "available": True,
                "valid": False,
                "reason": "non_monotonic",
                "word_count": len(sanitized),
                "coverage_percent": 0.0,
                "words": sanitized,
            }
        if duration_seconds > 0 and end > duration_seconds + 0.25:
            return {
                "available": True,
                "valid": False,
                "reason": "outside_audio_duration",
                "word_count": len(sanitized),
                "coverage_percent": 0.0,
                "words": sanitized,
            }
        previous_end = end
        covered += word

    normalized_covered = _normalized_text(covered)
    coverage = min(100.0, (len(normalized_covered) / max(1, len(expected))) * 100.0)
    if normalized_covered != expected:
        return {
            "available": True,
            "valid": False,
            "reason": "text_coverage_mismatch",
            "word_count": len(sanitized),
            "coverage_percent": round(coverage, 2),
            "words": sanitized,
        }

    confidence_values = [
        float(item["confidence"])
        for item in sanitized
        if isinstance(item.get("confidence"), (int, float))
        and math.isfinite(float(item["confidence"]))
    ]
    result = {
        "available": True,
        "valid": True,
        "reason": "ok",
        "word_count": len(sanitized),
        "coverage_percent": 100.0,
        "words": sanitized,
    }
    if confidence_values:
        result["mean_confidence"] = round(
            sum(confidence_values) / len(confidence_values), 4
        )
    return result


def load_alignment_cache(output_dir: Path) -> dict[str, Any]:
    path = output_dir / CACHE_FILENAME
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "entries": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": SCHEMA_VERSION, "entries": {}}
    entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
    if not isinstance(entries, dict):
        entries = {}
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": str(payload.get("updated_at", "") if isinstance(payload, dict) else ""),
        "entries": {
            str(key): value
            for key, value in entries.items()
            if isinstance(value, dict)
        },
    }


def _record_matches_entry(record: dict[str, Any], entry: dict[str, Any]) -> bool:
    text = _source_text(entry)
    if str(record.get("source_text_sha256") or "") != _source_fingerprint(text):
        return False
    cached_duration = record.get("duration_seconds")
    if cached_duration is None:
        return True
    try:
        current = _entry_duration(entry)
        cached = float(cached_duration)
    except (TypeError, ValueError):
        return False
    if current <= 0 or cached <= 0:
        return True
    return abs(current - cached) <= max(0.10, current * 0.02)


def apply_cached_word_alignments(
    entries: list[dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    cache = load_alignment_cache(output_dir)
    cached_entries = cache.get("entries", {})
    for entry in entries:
        text = _source_text(entry)
        duration = _entry_duration(entry)

        manifest_words = entry.get("word_alignments") or entry.get("words")
        manifest_diag = validate_word_alignment(text, duration, manifest_words)
        if manifest_diag["valid"]:
            entry["word_alignments"] = manifest_diag["words"]
            entry["_word_alignment"] = {
                key: value
                for key, value in manifest_diag.items()
                if key != "words"
            } | {"source": "manifest"}
            continue

        record = cached_entries.get(alignment_key(entry))
        if not isinstance(record, dict) or not _record_matches_entry(record, entry):
            entry.pop("word_alignments", None)
            entry["_word_alignment"] = {
                key: value
                for key, value in manifest_diag.items()
                if key != "words"
            } | {
                "source": "none",
                "reason": (
                    "stale_cache"
                    if isinstance(record, dict)
                    else manifest_diag.get("reason", "missing")
                ),
            }
            continue

        cached_diag = validate_word_alignment(text, duration, record.get("words"))
        if cached_diag["valid"]:
            entry["word_alignments"] = cached_diag["words"]
        else:
            entry.pop("word_alignments", None)
        entry["_word_alignment"] = {
            key: value
            for key, value in cached_diag.items()
            if key != "words"
        } | {
            "source": "cache",
            "provider": str(record.get("provider") or "import"),
            "updated_at": str(record.get("updated_at") or ""),
        }
    return entries


def _manifest_entries(output_dir: Path) -> list[dict[str, Any]]:
    path = output_dir / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError("Manifest file not found in project output directory")
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("Manifest entries must be a list")
    return [entry for entry in entries if isinstance(entry, dict)]


def alignment_diagnostics(output_dir: Path) -> dict[str, Any]:
    entries = apply_cached_word_alignments(_manifest_entries(output_dir), output_dir)
    rows = []
    usable = 0
    invalid = 0
    providers: dict[str, int] = {}
    for entry in entries:
        diag = dict(entry.get("_word_alignment") or {})
        valid = bool(diag.get("valid"))
        available = bool(diag.get("available"))
        if valid:
            usable += 1
            provider = str(diag.get("provider") or diag.get("source") or "unknown")
            providers[provider] = providers.get(provider, 0) + 1
        elif available:
            invalid += 1
        rows.append({
            "id": _entry_id(entry),
            "source_member_id": str(entry.get("source_member_id") or ""),
            "filename": str(entry.get("filename") or ""),
            "valid": valid,
            "available": available,
            "reason": str(diag.get("reason") or "missing"),
            "source": str(diag.get("source") or "none"),
            "provider": str(diag.get("provider") or ""),
            "word_count": int(diag.get("word_count") or 0),
            "coverage_percent": float(diag.get("coverage_percent") or 0.0),
            "mean_confidence": diag.get("mean_confidence"),
        })
    total = len(rows)
    return {
        "schema_version": SCHEMA_VERSION,
        "total": total,
        "usable": usable,
        "missing": max(0, total - usable - invalid),
        "invalid": invalid,
        "usable_percent": round((usable / total * 100.0) if total else 0.0, 2),
        "providers": providers,
        "entries": rows,
        "cache_file": str(output_dir / CACHE_FILENAME),
    }


def _entry_lookup(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    filename_counts: dict[str, int] = {}
    for entry in entries:
        filename = str(entry.get("filename") or "")
        if filename:
            filename_counts[filename] = filename_counts.get(filename, 0) + 1
    for entry in entries:
        item_id = _entry_id(entry)
        if item_id:
            lookup[f"id:{item_id}"] = entry
        member = str(entry.get("source_member_id") or "").replace("\\", "/").strip()
        if member:
            lookup[f"member:{member}"] = entry
        logical = str(entry.get("logical_id") or "").strip()
        if logical:
            lookup[f"logical:{logical}"] = entry
        filename = str(entry.get("filename") or "")
        if filename and filename_counts.get(filename) == 1:
            lookup[f"filename:{filename}"] = entry
    return lookup


def _resolve_import_entry(
    record: dict[str, Any],
    lookup: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = [
        ("source_member_id", "member"),
        ("logical_id", "logical"),
        ("id", "id"),
        ("index", "id"),
        ("filename", "filename"),
    ]
    for field, prefix in candidates:
        value = str(record.get(field) or "").replace("\\", "/").strip()
        if value:
            entry = lookup.get(f"{prefix}:{value}")
            if entry is not None:
                return entry
    return None


def import_word_alignments(output_dir: Path, payload: Any) -> dict[str, Any]:
    entries = _manifest_entries(output_dir)
    lookup = _entry_lookup(entries)
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("alignments") or payload.get("entries") or []
    else:
        records = []
    if not isinstance(records, list) or not records:
        raise ValueError("Alignment import must contain a non-empty alignments list")

    cache = load_alignment_cache(output_dir)
    cached_entries = dict(cache.get("entries", {}))
    imported: list[str] = []
    rejected: list[dict[str, str]] = []

    for raw in records:
        if not isinstance(raw, dict):
            rejected.append({"id": "", "reason": "record_not_object"})
            continue
        entry = _resolve_import_entry(raw, lookup)
        if entry is None:
            rejected.append({
                "id": str(raw.get("id") or raw.get("source_member_id") or raw.get("filename") or ""),
                "reason": "entry_not_found_or_ambiguous",
            })
            continue

        words = raw.get("words") or raw.get("word_alignments")
        duration = _entry_duration(entry)
        diag = validate_word_alignment(_source_text(entry), duration, words)
        item_id = _entry_id(entry)
        if not diag["valid"]:
            rejected.append({"id": item_id, "reason": str(diag["reason"])})
            continue

        key = alignment_key(entry)
        cached_entries[key] = {
            "id": item_id,
            "source_member_id": str(entry.get("source_member_id") or ""),
            "logical_id": str(entry.get("logical_id") or ""),
            "filename": str(entry.get("filename") or ""),
            "source_text_sha256": _source_fingerprint(_source_text(entry)),
            "duration_seconds": duration,
            "provider": str(raw.get("provider") or "import"),
            "updated_at": _now(),
            "words": diag["words"],
        }
        imported.append(item_id)

    result_payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now(),
        "entries": cached_entries,
    }
    atomic_write_text(
        output_dir / CACHE_FILENAME,
        json.dumps(result_payload, ensure_ascii=False, indent=2),
    )
    summary = alignment_diagnostics(output_dir)
    return {
        "ok": True,
        "imported_count": len(imported),
        "imported_ids": imported,
        "rejected_count": len(rejected),
        "rejected": rejected,
        "summary": {
            key: value
            for key, value in summary.items()
            if key != "entries"
        },
    }
