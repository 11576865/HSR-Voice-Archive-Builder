from __future__ import annotations

import csv
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from .wavpcm import parse_wav_pcm


def _read(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _identity(row: dict[str, str]) -> str:
    return (row.get("source_member_id") or "").replace("\\", "/").strip()


def load_training_rows(bilingual_csv: Path | None, manifest_csv: Path | None) -> list[dict[str, str]]:
    """Prefer final corrected English, preserving the package-relative audio identity."""
    manifest, corrected = _read(manifest_csv), _read(bilingual_csv)
    if not manifest and not corrected:
        raise FileNotFoundError("Completed archive manifest is missing")
    by_member = {_identity(r): r for r in corrected if _identity(r)}
    by_index = {(r.get("index") or "").strip(): r for r in corrected if (r.get("index") or "").strip()}
    if not manifest:
        return corrected
    result = []
    for row in manifest:
        member = _identity(row)
        final = by_member.get(member) if member else by_index.get((row.get("index") or "").strip())
        if final is None and member:
            candidate = by_index.get((row.get("index") or "").strip())
            if candidate and not _identity(candidate):
                final = candidate
        result.append({**row, **(final or {})})
    return result


def _source(root: Path, row: dict[str, str], counts: Counter[str]) -> Path | None:
    member = _identity(row)
    filename = (row.get("filename") or "").strip()
    if member:
        parts = PurePosixPath(member).parts
        if not parts or member.startswith("/") or any(part in {"..", "."} for part in parts):
            return None
        candidate = root.joinpath(*parts)
        return candidate if candidate.resolve().is_relative_to(root.resolve()) else None
    if not filename or counts[filename] != 1 or Path(filename).name != filename:
        return None
    matches = list(root.rglob(filename))
    return matches[0] if len(matches) == 1 else None


def export_gpt_sovits_dataset(
    rows: list[dict[str, str]], wav_root: Path, destination: Path, *,
    speaker: str, language: str = "en", copy_wav: bool = True, min_duration: float = 1.0,
) -> dict[str, Any]:
    if not rows:
        raise ValueError("Completed archive contains no training rows")
    if not wav_root.is_dir():
        raise FileNotFoundError(f"WAV directory not found: {wav_root}")
    if not speaker.strip() or speaker != speaker.strip(" .") or any(c in speaker for c in '<>:"/\\|?*\r\n') or speaker in {".", ".."}:
        raise ValueError("Invalid speaker name")
    if language != "en" or not copy_wav:
        raise ValueError("Only copied English WAV dataset export is supported")
    if min_duration < 0:
        raise ValueError("Minimum duration must not be negative")
    destination.mkdir(parents=True, exist_ok=True)
    raw_dir = destination / "raw"
    raw_dir.mkdir()
    rejected: list[dict[str, str]] = []
    list_rows: list[str] = []
    counts = Counter((row.get("filename") or "").strip() for row in rows)
    for position, row in enumerate(rows, 1):
        index = str(row.get("index") or position)
        filename = _identity(row) or (row.get("filename") or "").strip()
        text = (row["source_text"] if "source_text" in row else row.get("english") or "").strip()
        source = _source(wav_root, row, counts)
        reason = ""
        if source is None:
            reason = "ambiguous_or_unsafe_audio_identity"
        elif not source.is_file():
            reason = "missing_audio"
        elif not text:
            reason = "missing_text"
        elif any(c in text for c in ("|", "\r", "\n")):
            reason = "invalid_text"
        else:
            try:
                info = parse_wav_pcm(source)
                if source.stat().st_size < info.data_offset + info.data_size:
                    reason = "unreadable_audio"
                elif info.frames / info.sample_rate < min_duration:
                    reason = "too_short"
            except (OSError, ValueError, ZeroDivisionError):
                reason = "unreadable_audio"
        if reason:
            rejected.append({"index": index, "reason": reason, "file": filename})
            continue
        target = raw_dir / f"{len(list_rows) + 1:06d}.wav"
        try:
            shutil.copy2(source, target)
        except OSError:
            rejected.append({"index": index, "reason": "unreadable_audio", "file": filename})
            continue
        list_rows.append(f"{target.resolve()}|{speaker}|{language}|{text}")
    list_file = destination / f"{speaker}.list"
    list_file.write_text("\n".join(list_rows) + ("\n" if list_rows else ""), encoding="utf-8")
    with (destination / "rejected.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["index", "reason", "file"])
        writer.writeheader()
        writer.writerows(rejected)
    report = {
        "speaker": speaker, "language": language, "format": "GPT-SoVITS WebUI dataset",
        "total": len(rows), "exported": len(list_rows), "rejected": len(rejected),
        "list_file": str(list_file.resolve()), "output": str(destination.resolve()),
    }
    (destination / "dataset_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "README_GPTSoVITS.txt").write_text(
        "Generated by HSR Voice Archive Builder.\n"
        "Corrected English source text takes priority over manifest text.\n"
        "Open GPT-SoVITS WebUI and select the .list file for dataset formatting.\n"
        "See rejected.csv for missing, unreadable, short or unmatched samples.\n",
        encoding="utf-8",
    )
    return report


def export_project_dataset(config: Any, *, speaker: str = "", language: str = "en") -> dict[str, Any]:
    """Shared export operation for the FastAPI and stdlib control servers."""
    from .builder import ensure_dir_or_extract
    from .project import resolve_project_path

    wav_path = resolve_project_path(config, config.wav_source)
    output = resolve_project_path(config, config.output_dir)
    if wav_path is None or output is None:
        raise ValueError("Project paths are incomplete")
    corrected = output / "bilingual_index_corrected.csv"
    manifest = output / "manifest.csv"
    if not manifest.is_file() and not corrected.is_file():
        raise FileNotFoundError("Build the archive before exporting a training dataset")
    rows = load_training_rows(corrected, manifest)
    name = (speaker or config.name).strip()
    if not name or name != name.strip(" .") or any(c in name for c in '<>:"/\\|?*\r\n'):
        raise ValueError("Invalid speaker name")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"{name}_GPTSoVITS"
    with tempfile.TemporaryDirectory(prefix=".gpt_sovits_", dir=output) as temp:
        working = Path(temp)
        wav_root = ensure_dir_or_extract(wav_path, working, "source_wavs")
        staging = working / destination.name
        report = export_gpt_sovits_dataset(rows, wav_root, staging, speaker=name, language=language)
        list_file = staging / f"{name}.list"
        list_file.write_text(
            list_file.read_text(encoding="utf-8").replace(str(staging.resolve()), str(destination.resolve())),
            encoding="utf-8",
        )
        report["output"] = str(destination.resolve())
        report["list_file"] = str((destination / f"{name}.list").resolve())
        (staging / "dataset_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        backup = working / "previous_dataset"
        if destination.exists():
            destination.rename(backup)
        try:
            staging.rename(destination)
        except BaseException:
            if backup.exists():
                backup.rename(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    return report
