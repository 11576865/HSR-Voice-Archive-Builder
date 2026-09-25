from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import Any

from .builder import (
    atomic_write_text,
    collect_wav_members,
    ensure_dir_or_extract,
    resolve_member_wav,
)
from .project import ProjectConfig, resolve_project_path
from .wavpcm import parse_wav_pcm


SCHEMA_VERSION = 1
ANNOTATIONS_FILENAME = "reference_annotations.json"


def _annotation_path(config: ProjectConfig) -> Path:
    return Path(config.root).resolve() / ANNOTATIONS_FILENAME


def load_reference_annotations(config: ProjectConfig) -> dict[str, dict[str, Any]]:
    path = _annotation_path(config)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
    if not isinstance(entries, dict):
        return {}
    return {
        str(key): value
        for key, value in entries.items()
        if isinstance(value, dict)
    }


def _entry_id(entry: dict[str, Any]) -> str:
    value = entry.get("index")
    if value is None:
        value = entry.get("id") or entry.get("filename") or ""
    return str(value)


def _annotation_key(entry: dict[str, Any]) -> str:
    member = str(entry.get("source_member_id") or "").replace("\\", "/").strip()
    if member:
        return f"member:{member}"
    logical = str(entry.get("logical_id") or "").strip()
    if logical:
        return f"logical:{logical}"
    return f"id:{_entry_id(entry)}"


def _load_manifest_entries(output_dir: Path) -> list[dict[str, Any]]:
    manifest = output_dir / "manifest.json"
    if not manifest.is_file():
        raise FileNotFoundError("Build the archive before using the reference workbench")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    entries = payload.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("Manifest entries must be a list")
    return [entry for entry in entries if isinstance(entry, dict)]


def _find_entry(output_dir: Path, item_id: str | int) -> dict[str, Any]:
    wanted = str(item_id)
    for entry in _load_manifest_entries(output_dir):
        if _entry_id(entry) == wanted:
            return entry
    raise KeyError(f"Subtitle entry not found: {item_id}")


def decorate_subtitles(
    config: ProjectConfig,
    output_dir: Path | None,
    subtitles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if output_dir is None:
        return subtitles
    annotations = load_reference_annotations(config)
    for subtitle in subtitles:
        key = str(subtitle.get("reference_key") or "")
        if not key:
            member = str(subtitle.get("source_member_id") or "").replace("\\", "/").strip()
            logical = str(subtitle.get("logical_id") or "").strip()
            key = (
                f"member:{member}" if member
                else f"logical:{logical}" if logical
                else f"id:{subtitle.get('id', '')}"
            )
        ann = annotations.get(key, {})
        subtitle["reference_key"] = key
        subtitle["reference_selected"] = bool(ann.get("selected", False))
        subtitle["reference_emotion"] = str(ann.get("emotion", "") or "")
        subtitle["reference_intensity"] = float(ann.get("intensity", 0.5) or 0.5)
        subtitle["reference_quality"] = str(ann.get("quality", "") or "")
    return subtitles


def save_reference_annotation(
    config: ProjectConfig,
    output_dir: Path,
    update: dict[str, Any],
) -> dict[str, Any]:
    item_id = update.get("id")
    if item_id is None:
        raise ValueError("Reference annotation requires subtitle id")
    entry = _find_entry(output_dir, item_id)
    key = _annotation_key(entry)

    selected = bool(update.get("selected", False))
    emotion = str(update.get("emotion", "") or "").strip()
    if len(emotion) > 64:
        raise ValueError("Emotion tag is too long")
    try:
        intensity = float(update.get("intensity", 0.5))
    except (TypeError, ValueError) as exc:
        raise ValueError("Intensity must be a number") from exc
    if not 0.0 <= intensity <= 1.0:
        raise ValueError("Intensity must be between 0 and 1")
    quality = str(update.get("quality", "") or "").strip().lower()
    if quality not in {"", "good", "ok", "poor"}:
        raise ValueError("Quality must be one of: good, ok, poor")

    entries = load_reference_annotations(config)
    annotation = {
        "subtitle_id": _entry_id(entry),
        "source_member_id": str(entry.get("source_member_id") or ""),
        "logical_id": str(entry.get("logical_id") or ""),
        "filename": str(entry.get("filename") or ""),
        "selected": selected,
        "emotion": emotion,
        "intensity": intensity,
        "quality": quality,
    }
    entries[key] = annotation
    payload = {
        "schema_version": SCHEMA_VERSION,
        "entries": entries,
    }
    atomic_write_text(
        _annotation_path(config),
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    return {"key": key, "annotation": annotation}


def _preview_root(config: ProjectConfig) -> Path:
    source = resolve_project_path(config, config.wav_source)
    if source is None:
        raise ValueError("Project WAV source is not configured")
    source = source.resolve()
    if source.is_dir():
        return source

    state = resolve_project_path(config, config.state_dir)
    if state is None:
        state = Path(config.root).resolve() / ".state"
    cache = state / "reference-preview"
    marker = cache / "source.json"
    root = cache / "source_wavs"
    fingerprint = {
        "path": str(source),
        "size": source.stat().st_size if source.is_file() else None,
        "mtime_ns": source.stat().st_mtime_ns if source.is_file() else None,
    }
    if marker.is_file() and root.is_dir():
        try:
            if json.loads(marker.read_text(encoding="utf-8")) == fingerprint:
                return root
        except (OSError, ValueError):
            pass

    if cache.exists():
        shutil.rmtree(cache)
    cache.mkdir(parents=True, exist_ok=True)
    extracted = ensure_dir_or_extract(source, cache, "source_wavs")
    atomic_write_text(marker, json.dumps(fingerprint, ensure_ascii=False, indent=2))
    return extracted


def resolve_reference_audio(
    config: ProjectConfig,
    output_dir: Path,
    item_id: str | int,
) -> tuple[Path, dict[str, Any]]:
    entry = _find_entry(output_dir, item_id)
    root = _preview_root(config)
    members = collect_wav_members(root)
    audio, resolved_member = resolve_member_wav(
        members,
        str(entry.get("filename") or ""),
        str(entry.get("source_member_id") or ""),
    )
    if audio is None or not audio.is_file():
        raise FileNotFoundError(
            f"Original WAV not found for subtitle {item_id}: "
            f"{resolved_member or entry.get('filename') or ''}"
        )
    return audio, entry


def export_reference_pack(
    config: ProjectConfig,
    output_dir: Path,
    *,
    speaker: str = "",
) -> dict[str, Any]:
    name = (speaker or config.name).strip()
    if (
        not name
        or name != name.strip(" .")
        or any(c in name for c in '<>:"/\\|?*\r\n')
        or name in {".", ".."}
    ):
        raise ValueError("Invalid speaker name")

    annotations = load_reference_annotations(config)
    selected = [
        (key, item)
        for key, item in annotations.items()
        if bool(item.get("selected"))
    ]
    if not selected:
        raise ValueError("No reference audio has been selected")

    entries = _load_manifest_entries(output_dir)
    by_key = {_annotation_key(entry): entry for entry in entries}
    root = _preview_root(config)
    members = collect_wav_members(root)
    destination = output_dir / f"{name}_ReferencePack"
    staging = output_dir / f".{name}_ReferencePack.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    audio_dir = staging / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    catalog: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    outside_recommended = 0

    for key, annotation in selected:
        entry = by_key.get(key)
        if entry is None:
            rejected.append({
                "subtitle_id": str(annotation.get("subtitle_id") or ""),
                "reason": "manifest_entry_not_found",
                "file": str(annotation.get("filename") or ""),
            })
            continue
        try:
            audio, _resolved_member = resolve_member_wav(
                members,
                str(entry.get("filename") or ""),
                str(entry.get("source_member_id") or ""),
            )
            if audio is None or not audio.is_file():
                raise FileNotFoundError(entry.get("source_member_id") or entry.get("filename") or "")
            info = parse_wav_pcm(audio)
            duration = info.frames / info.sample_rate
        except Exception:
            rejected.append({
                "subtitle_id": _entry_id(entry),
                "reason": "audio_unavailable_or_unreadable",
                "file": str(entry.get("source_member_id") or entry.get("filename") or ""),
            })
            continue

        target = audio_dir / f"{len(catalog) + 1:06d}.wav"
        shutil.copy2(audio, target)
        recommended = 3.0 <= duration <= 10.0
        if not recommended:
            outside_recommended += 1
        catalog.append({
            "id": f"{len(catalog) + 1:06d}",
            "audio": target.relative_to(staging).as_posix(),
            "subtitle_id": _entry_id(entry),
            "source_member_id": str(entry.get("source_member_id") or ""),
            "filename": str(entry.get("filename") or ""),
            "text": str(entry.get("source_text") or entry.get("english") or ""),
            "language": config.source_text_language or "en",
            "duration_seconds": round(duration, 3),
            "recommended_duration": recommended,
            "emotion": str(annotation.get("emotion") or ""),
            "intensity": float(annotation.get("intensity", 0.5) or 0.5),
            "quality": str(annotation.get("quality") or ""),
        })

    (staging / "reference_catalog.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "speaker": name,
                "source_project": config.name,
                "references": catalog,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with (staging / "rejected.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["subtitle_id", "reason", "file"])
        writer.writeheader()
        writer.writerows(rejected)
    (staging / "README_REFERENCE_PACK.txt").write_text(
        "Generated by HSR Voice Archive Builder.\n"
        "Reference audio remains paired with the official source transcript.\n"
        "Emotion/intensity/quality are human annotations, not official metadata.\n"
        "GPT-SoVITS commonly works best with clear 3-10 second reference clips;\n"
        "clips outside that range are retained but flagged in reference_catalog.json.\n",
        encoding="utf-8",
    )

    if destination.exists():
        shutil.rmtree(destination)
    staging.rename(destination)
    return {
        "speaker": name,
        "selected": len(selected),
        "exported": len(catalog),
        "rejected": len(rejected),
        "outside_recommended_duration": outside_recommended,
        "output": str(destination.resolve()),
        "catalog": str((destination / "reference_catalog.json").resolve()),
    }
