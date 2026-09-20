from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from .builder import MAX_ARCHIVE_MEMBERS, atomic_write_text
from .credentials import credentials_status
from .identity import parse_voice_identity
from .project import ProjectConfig, create_project, update_project
from .schema import normalize_index
from .translator import DEFAULT_MODEL

SUPPORTED_ARCHIVES = {".zip", ".7z"}
MAX_SCAN_FILES = 100_000

_STOP_TOKENS = {
    "archive", "vo", "avatar", "audio", "voice", "chapter", "companion", "side",
    "cast", "skill", "maze", "turn", "begin", "battle", "combat", "open", "chest",
    "preciouschest", "waiting", "revive", "hit", "heavy", "light", "threat", "lookat",
    "solve", "puzzle", "ultra", "select", "high", "low", "male", "female",
}


def _safe_member_name(name: str) -> str:
    normalized = str(name or "").replace("\\", "/").strip()
    if not normalized or normalized.startswith("/"):
        raise ValueError(f"Unsafe archive member path: {name!r}")
    parts = PurePosixPath(normalized).parts
    if not parts or ".." in parts or any(part == "" for part in parts):
        raise ValueError(f"Unsafe archive member path: {name!r}")
    if ":" in parts[0]:
        raise ValueError(f"Unsafe archive member drive path: {name!r}")
    return normalized


def _parse_7z_slt(text: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    in_entries = False
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if line.startswith("----------"):
            in_entries = True
            current = {}
            continue
        if not in_entries:
            continue
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if " = " in line:
            key, value = line.split(" = ", 1)
            current[key.strip()] = value.strip()
    if current:
        records.append(current)
    return records


def source_inventory(source: Path) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    files: list[dict[str, Any]] = []
    if source.is_dir():
        for idx, path in enumerate(source.rglob("*"), 1):
            if idx > MAX_SCAN_FILES:
                raise ValueError(f"Source contains more than {MAX_SCAN_FILES} filesystem entries")
            if not path.is_file():
                continue
            rel = path.relative_to(source).as_posix()
            files.append({"name": rel, "size": path.stat().st_size})
    elif source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as z:
            infos = z.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ValueError(f"ZIP has too many members: {len(infos)}")
            for info in infos:
                if info.is_dir():
                    continue
                files.append({
                    "name": _safe_member_name(info.filename),
                    "size": max(0, int(info.file_size)),
                })
    elif source.is_file() and source.suffix.lower() == ".7z":
        exe = shutil.which("7zz") or shutil.which("7z")
        if not exe:
            raise RuntimeError("7-Zip CLI is required to scan .7z packages")
        listed = subprocess.run(
            [exe, "l", "-slt", str(source)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            check=False,
        )
        if listed.returncode != 0:
            raise RuntimeError(
                f"7-Zip listing failed ({listed.returncode}): {listed.stderr.strip()}"
            )
        records = _parse_7z_slt(listed.stdout)
        if len(records) > MAX_ARCHIVE_MEMBERS:
            raise ValueError(f"7z has too many members: {len(records)}")
        for record in records:
            name = record.get("Path", "")
            if not name:
                continue
            attrs = record.get("Attributes", "")
            if attrs.startswith("D"):
                continue
            try:
                size = int(record.get("Size", "0") or "0")
            except ValueError:
                size = 0
            files.append({"name": _safe_member_name(name), "size": max(0, size)})
    else:
        raise ValueError("Quick mode accepts a directory, .zip, or .7z source")

    wavs = [Path(item["name"]).name for item in files if str(item["name"]).lower().endswith(".wav")]
    labs = [Path(item["name"]).stem for item in files if str(item["name"]).lower().endswith(".lab")]
    wav_stems = {Path(name).stem for name in wavs}
    lab_stems = set(labs)

    return {
        "source": str(source),
        "kind": "directory" if source.is_dir() else source.suffix.lower().lstrip("."),
        "file_count": len(files),
        "wav_count": len(wavs),
        "lab_count": len(labs),
        "wav_names": wavs,
        "wav_lab_pairs": len(wav_stems & lab_stems),
        "wav_without_lab": len(wav_stems - lab_stems),
        "declared_bytes": sum(int(item.get("size", 0)) for item in files),
    }


def _candidate_roots(source: Path) -> list[Path]:
    home = Path.home()
    roots = [
        source.parent if source.is_file() else source,
        home / "storage" / "downloads",
        home / "storage" / "shared" / "Download",
        Path("/storage/emulated/0/Download"),
        Path.cwd(),
    ]
    result: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.expanduser().resolve())
        except OSError:
            key = str(root.expanduser())
        if key not in seen:
            seen.add(key)
            result.append(root)
    return result


def _index_candidates(source: Path, wav_names: set[str]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in _candidate_roots(source):
        if not root.is_dir():
            continue
        try:
            csvs = list(root.glob("*.csv"))
        except (OSError, PermissionError):
            continue
        for path in csvs:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if str(resolved) in seen:
                continue
            seen.add(str(resolved))
            try:
                rows = normalize_index(path)
            except Exception:
                continue
            by_name = {Path(row["filename"]).name: row for row in rows}
            matched = wav_names & set(by_name)
            if not matched:
                continue
            english_matched = sum(
                bool(str(by_name[name].get("english", "")).strip())
                for name in matched
            )
            coverage = len(matched) / max(1, len(wav_names))
            name_bonus = 0
            lower = path.name.lower()
            if "完整索引" in path.name:
                name_bonus += 5
            if "379" in lower:
                name_bonus += 2
            if "index" in lower or "索引" in path.name:
                name_bonus += 2
            score = coverage * 1000 + english_matched + name_bonus
            candidates.append({
                "path": str(resolved),
                "row_count": len(rows),
                "matched_wavs": len(matched),
                "english_matched": english_matched,
                "coverage": round(coverage, 6),
                "score": score,
            })
    candidates.sort(key=lambda x: (-float(x["score"]), str(x["path"])))
    return candidates


def infer_character(wav_names: list[str]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    total = max(1, len(wav_names))
    for name in wav_names:
        stem = Path(name).stem.casefold()
        tokens = re.findall(r"[a-z][a-z0-9]{2,}", stem)
        for token in set(tokens):
            if token in _STOP_TOKENS or token.isdigit() or re.fullmatch(r"chapter\d+", token):
                continue
            if len(token) >= 4:
                counts[token] += 1

    if not counts:
        return {"value": "", "confidence": "none", "share": 0.0}
    token, count = counts.most_common(1)[0]
    share = count / total
    confidence = "high" if share >= 0.75 else ("medium" if share >= 0.4 else "low")
    return {
        "value": token,
        "confidence": confidence,
        "share": round(share, 4),
        "top_candidates": counts.most_common(5),
    }


def quick_scan(english_source: Path, chs_source: Path | None = None) -> dict[str, Any]:
    english = source_inventory(english_source)
    blockers: list[str] = []
    warnings: list[str] = []

    if english["wav_count"] == 0:
        blockers.append("No WAV files were found in the English source")

    wav_names = {Path(name).name for name in english["wav_names"]}
    indexes = _index_candidates(Path(english["source"]), wav_names)
    selected_index = indexes[0] if indexes else None

    if selected_index is None:
        blockers.append(
            "No local index CSV covers the package WAV names; reliable playback order is not available yet"
        )
    else:
        if int(selected_index["matched_wavs"]) != int(english["wav_count"]):
            blockers.append(
                f"Best local index covers only {selected_index['matched_wavs']} / "
                f"{english['wav_count']} WAV files"
            )
        if int(selected_index["english_matched"]) != int(english["wav_count"]):
            blockers.append(
                f"Best local index has English text for only {selected_index['english_matched']} / "
                f"{english['wav_count']} WAV files"
            )

    chs = None
    if chs_source is not None and str(chs_source).strip():
        chs = source_inventory(chs_source)
        if chs["lab_count"] == 0:
            warnings.append("Chinese source contains no LAB files")

    character = infer_character(list(wav_names))
    if character["confidence"] == "low":
        warnings.append("Character inference confidence is low; review before building")

    api = credentials_status()
    if not api["configured"]:
        warnings.append("AI translation API is not configured; unmatched Chinese text will remain missing")

    return {
        "schema_version": 1,
        "kind": "quick_scan",
        "english": {
            key: value for key, value in english.items() if key != "wav_names"
        },
        "chinese": (
            {key: value for key, value in chs.items() if key != "wav_names"}
            if chs else None
        ),
        "character": character,
        "index": selected_index,
        "index_candidates": indexes[:8],
        "translation": {
            "provider": api["provider"],
            "base_url": api["base_url"],
            "configured": api["configured"],
            "model": DEFAULT_MODEL,
        },
        "blockers": blockers,
        "warnings": warnings,
        "ready": not blockers,
    }


def _safe_project_name(plan: dict[str, Any], source: Path) -> str:
    char = str(plan.get("character", {}).get("value", "")).strip()
    return char or source.stem or "voice-archive"


def _default_project_root(plan: dict[str, Any], source: Path) -> Path:
    base = Path.home() / "HSR-Voice-Projects"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", _safe_project_name(plan, source)).strip("-")
    stem = stem or "voice-archive"
    candidate = base / stem
    if not candidate.exists():
        return candidate
    for i in range(2, 1000):
        alt = base / f"{stem}-{i}"
        if not alt.exists():
            return alt
    raise RuntimeError("Could not allocate a unique quick-project directory")


def create_quick_project(
    english_source: Path,
    *,
    chs_source: Path | None = None,
    root: Path | None = None,
    name: str = "",
) -> tuple[ProjectConfig, dict[str, Any]]:
    english_source = english_source.expanduser().resolve()
    chs_source = chs_source.expanduser().resolve() if chs_source else None
    plan = quick_scan(english_source, chs_source)
    if not plan["ready"]:
        raise RuntimeError("Quick scan has blockers: " + "; ".join(plan["blockers"]))

    selected_index = plan["index"]
    assert selected_index is not None
    original_rows = normalize_index(Path(selected_index["path"]))
    wanted = set(source_inventory(english_source)["wav_names"])
    filtered = [row for row in original_rows if Path(row["filename"]).name in wanted]
    if len(filtered) != len(wanted):
        raise RuntimeError("Index coverage changed between scan and project creation")

    project_root = (root or _default_project_root(plan, english_source)).expanduser().resolve()
    generated_dir = project_root / ".generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    generated_index = generated_dir / "quick_index.csv"

    fields = ["index", "group", "filename", "source", "source_detail", "english", "sha256"]
    with generated_index.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(filtered)

    config = create_project(
        project_root,
        name=name.strip() or _safe_project_name(plan, english_source),
        index_csv=str(generated_index),
        wav_source=str(english_source),
        output_dir="output",
        chs_source=str(chs_source) if chs_source else "",
        remote_character=str(plan.get("character", {}).get("value", "")),
    )
    update_project(
        config,
        make_flac=True,
        translate_missing=bool(plan["translation"]["configured"]),
        translation_model=DEFAULT_MODEL,
    )

    atomic_write_text(
        project_root / ".generated" / "quick_scan.json",
        json.dumps(plan, ensure_ascii=False, indent=2),
    )
    return config, plan
