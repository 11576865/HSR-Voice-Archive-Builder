from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from .builder import MAX_ARCHIVE_MEMBERS, atomic_write_text, sha256_file
from .credentials import credentials_status
from .identity import parse_voice_identity
from .project import ProjectConfig, create_project, update_project
from .remote_index import (
    DEFAULT_EN_INDEX_URL,
    ai_hobbyist_index_label,
    ai_hobbyist_index_url,
    fetch_ai_hobbyist_index_for_filenames_cached,
)
from .schema import normalize_index
from .translation_runtime import estimate_workload_tokens
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
    source_stat = source.stat()
    content_hash = hashlib.sha256()
    if source.is_dir():
        for idx, path in enumerate(sorted(source.rglob("*"), key=lambda p: p.as_posix()), 1):
            if idx > MAX_SCAN_FILES:
                raise ValueError(f"Source contains more than {MAX_SCAN_FILES} filesystem entries")
            if path.is_symlink():
                raise ValueError(f"Directory source contains a symbolic link: {path}")
            if not path.is_file():
                continue
            rel = path.relative_to(source).as_posix()
            size = path.stat().st_size
            file_hash = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    file_hash.update(chunk)
            digest = file_hash.hexdigest()
            content_hash.update(rel.encode("utf-8"))
            content_hash.update(b"\0")
            content_hash.update(str(size).encode("ascii"))
            content_hash.update(b"\0")
            content_hash.update(digest.encode("ascii"))
            content_hash.update(b"\n")
            files.append({"name": rel, "size": size})
    elif source.is_file() and source.suffix.lower() == ".zip":
        with source.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                content_hash.update(chunk)
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
        with source.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                content_hash.update(chunk)
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
    duplicate_wavs = sorted(name for name, count in Counter(wavs).items() if count > 1)

    return {
        "source": str(source),
        "kind": "directory" if source.is_dir() else source.suffix.lower().lstrip("."),
        "file_count": len(files),
        "wav_count": len(wavs),
        "lab_count": len(labs),
        "wav_names": wavs,
        "lab_names": labs,
        "duplicate_wav_names": duplicate_wavs,
        "wav_lab_pairs": len(wav_stems & lab_stems),
        "wav_without_lab": len(wav_stems - lab_stems),
        "declared_bytes": sum(int(item.get("size", 0)) for item in files),
        "fingerprint": {
            "algorithm": "tree-sha256-v1" if source.is_dir() else "sha256",
            "digest": content_hash.hexdigest(),
            "source_size_bytes": source_stat.st_size,
            "source_modified_ns": source_stat.st_mtime_ns,
        },
    }


def discover_source_candidates() -> list[dict[str, Any]]:
    """List likely local voice-package archives without opening or uploading them."""
    home = Path.home()
    roots = [
        home / "storage" / "downloads",
        home / "storage" / "shared" / "Download",
        Path("/storage/emulated/0/Download"),
        Path.cwd(),
    ]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        try:
            if not root.is_dir():
                continue
            candidates = [
                p for p in root.iterdir()
                if p.is_file() and p.suffix.lower() in SUPPORTED_ARCHIVES
            ]
        except (OSError, PermissionError):
            continue
        for path in candidates:
            try:
                resolved = path.resolve()
                key = str(resolved)
                stat = resolved.stat()
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            lower = path.name.casefold()
            role_hint = (
                "english" if re.search(r"(^|[^a-z])(en|english)([^a-z]|$)", lower)
                else ("chinese" if any(x in lower for x in ("chs", "chinese", "中文")) else "")
            )
            result.append({
                "path": key,
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
                "role_hint": role_hint,
            })
    result.sort(key=lambda item: (-int(item["modified_ns"]), str(item["name"])))
    return result


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
                "file_sha256": sha256_file(resolved),
                "size_bytes": resolved.stat().st_size,
                "modified_ns": resolved.stat().st_mtime_ns,
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


def _index_fingerprint(rows: list[dict[str, str]]) -> str:
    canonical = [
        {
            "filename": Path(str(row.get("filename", ""))).name,
            "english": str(row.get("english", "")).strip(),
            "hash": str(row.get("hash", row.get("sha256", ""))).strip().lower(),
        }
        for row in rows
    ]
    raw = json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _remote_candidate(
    records: list[dict[str, str]],
    wav_names: set[str],
    *,
    url: str,
    cache: dict[str, Any],
    source_text_language: str = "en",
) -> dict[str, Any]:
    by_name: dict[str, dict[str, str]] = {}
    duplicates: set[str] = set()
    for row in records:
        name = Path(str(row.get("filename", ""))).name
        if not name:
            continue
        if name in by_name:
            duplicates.add(name)
        by_name[name] = row
    matched = wav_names & set(by_name)
    english_matched = sum(bool(str(by_name[name].get("english", "")).strip()) for name in matched)
    character_counts = Counter(
        str(by_name[name].get("character", "")).strip()
        for name in matched
        if str(by_name[name].get("character", "")).strip()
    )
    primary_character, primary_count = character_counts.most_common(1)[0] if character_counts else ("", 0)
    return {
        "source": "remote",
        "provider": ai_hobbyist_index_label(url),
        "source_text_language": source_text_language,
        "url": url,
        "row_count": len(records),
        "matched_wavs": len(matched),
        "english_matched": english_matched,
        "coverage": round(len(matched) / max(1, len(wav_names)), 6),
        "duplicate_filenames": sorted(duplicates),
        "characters": sorted(character_counts),
        "character_counts": dict(character_counts.most_common()),
        "primary_character": primary_character,
        "primary_character_share": round(primary_count / max(1, len(matched)), 6),
        "order_basis": "workbook_row_order",
        "records_fingerprint": _index_fingerprint(records),
        "cache": cache,
    }


def _remote_rows(records: list[dict[str, str]], wanted: set[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for pos, row in enumerate(records, 1):
        filename = Path(str(row.get("filename", ""))).name
        if filename not in wanted:
            continue
        if filename in seen:
            raise RuntimeError(f"Remote index has a duplicate filename: {filename}")
        seen.add(filename)
        remote_hash = str(row.get("hash", "")).strip().lower()
        result.append({
            "index": str(pos),
            "group": parse_voice_identity(filename).group,
            "filename": filename,
            "source": "AI-Hobbyist",
            "source_detail": str(row.get("character", "")).strip(),
            "english": str(row.get("english", "")).strip(),
            "sha256": remote_hash if re.fullmatch(r"[0-9a-f]{64}", remote_hash) else "",
        })
    return result


def quick_scan(
    english_source: Path,
    chs_source: Path | None = None,
    *,
    reference_source: Path | None = None,
    source_text_language: str = "en",
    remote_index_url: str = "",
) -> dict[str, Any]:
    remote_index_url = remote_index_url.strip() or ai_hobbyist_index_url(source_text_language)
    english = source_inventory(english_source)
    blockers: list[str] = []
    warnings: list[str] = []

    if english["wav_count"] == 0:
        blockers.append("No WAV files were found in the primary audio source")
    if english.get("duplicate_wav_names"):
        blockers.append(
            "Duplicate WAV basenames were found in the primary source: "
            + ", ".join(english["duplicate_wav_names"][:5])
        )
    if source_text_language != "en" and int(english.get("wav_lab_pairs", 0)) < int(english.get("wav_count", 0)):
        blockers.append(
            f"Source text language is {source_text_language}, but the primary package has "
            f"LAB text for only {english.get('wav_lab_pairs', 0)} / {english.get('wav_count', 0)} WAV files"
        )

    wav_names = {Path(name).name for name in english["wav_names"]}
    indexes = _index_candidates(Path(english["source"]), wav_names)
    selected_index = indexes[0] if indexes else None
    if selected_index is not None:
        selected_index = {**selected_index, "source": "local"}

    character = infer_character(list(wav_names))
    local_complete = bool(
        selected_index
        and int(selected_index["matched_wavs"]) == int(english["wav_count"])
        and int(selected_index["english_matched"]) == int(english["wav_count"])
    )
    remote_attempt: dict[str, Any] | None = None
    remote_records: list[dict[str, str]] = []
    if not local_complete and not blockers:
        try:
            remote_records, cache = fetch_ai_hobbyist_index_for_filenames_cached(
                wav_names, remote_index_url
            )
            remote_attempt = _remote_candidate(
                remote_records,
                wav_names,
                url=remote_index_url,
                cache=cache,
                source_text_language=source_text_language,
            )
            if cache.get("stale"):
                warnings.append("Remote index refresh failed; a stale cached copy was used")
            remote_complete = (
                not remote_attempt["duplicate_filenames"]
                and float(remote_attempt["primary_character_share"]) >= 0.75
                and int(remote_attempt["matched_wavs"]) == int(english["wav_count"])
                and int(remote_attempt["english_matched"]) == int(english["wav_count"])
            )
            if remote_complete:
                selected_index = remote_attempt
        except Exception as exc:
            warnings.append(f"Remote index fallback failed: {type(exc).__name__}: {exc}")

    selected_complete = bool(
        selected_index
        and int(selected_index["matched_wavs"]) == int(english["wav_count"])
        and int(selected_index["english_matched"]) == int(english["wav_count"])
        and not selected_index.get("duplicate_filenames")
        and (
            selected_index.get("source") != "remote"
            or float(selected_index.get("primary_character_share", 0.0)) >= 0.75
        )
    )
    if not selected_complete:
        if selected_index is None:
            blockers.append("No reliable local or remote index covers the package WAV names")
        else:
            if selected_index.get("duplicate_filenames"):
                blockers.append("The candidate index contains duplicate filenames")
            if (
                selected_index.get("source") == "remote"
                and float(selected_index.get("primary_character_share", 0.0)) < 0.75
            ):
                blockers.append("Remote index matches do not have a sufficiently dominant character identity")
            if int(selected_index["matched_wavs"]) != int(english["wav_count"]):
                blockers.append(
                    f"Best index covers only {selected_index['matched_wavs']} / "
                    f"{english['wav_count']} WAV files"
                )
            if int(selected_index["english_matched"]) != int(english["wav_count"]):
                blockers.append(
                    f"Best index has source text for only {selected_index['english_matched']} / "
                    f"{english['wav_count']} WAV files"
                )

    chs = None
    if chs_source is not None and str(chs_source).strip():
        if Path(chs_source).expanduser().resolve() == Path(english["source"]).resolve():
            warnings.append("Primary audio and target-text source point to the same package")
        chs = source_inventory(chs_source)
        if chs["lab_count"] == 0:
            warnings.append("Target-text source contains no LAB files")

    reference = None
    if reference_source is not None and str(reference_source).strip():
        reference = source_inventory(reference_source)
        if reference["lab_count"] == 0:
            warnings.append(
                "Reference package contains no LAB text; audio-only reference transcription "
                "is not implemented yet"
            )

    if character["confidence"] == "low":
        warnings.append("Character inference confidence is low; review before building")

    api = credentials_status()
    if not api["configured"]:
        warnings.append("AI translation API is not configured; unmatched target text will remain missing")

    pending_records: list[dict[str, str]] = []
    official_chinese_matches = 0
    if selected_complete and selected_index is not None:
        if selected_index.get("source") == "remote":
            index_rows = _remote_rows(remote_records, wav_names)
        else:
            index_rows = [
                row for row in normalize_index(Path(selected_index["path"]))
                if Path(str(row.get("filename", ""))).name in wav_names
            ]
        chinese_stems = set(chs.get("lab_names", [])) if chs else set()
        official_chinese_matches = sum(
            Path(str(row.get("filename", ""))).stem in chinese_stems
            for row in index_rows
        )
        pending_records = [
            {
                "id": Path(str(row.get("filename", ""))).name,
                "english": str(row.get("english", "")).strip(),
            }
            for row in index_rows
            if Path(str(row.get("filename", ""))).stem not in chinese_stems
        ]
    translation_estimate = estimate_workload_tokens(pending_records, 80)

    return {
        "schema_version": 2,
        "kind": "quick_scan",
        "source_text_language": source_text_language,
        "english": {
            key: value for key, value in english.items() if key not in {"wav_names", "lab_names"}
        },
        "chinese": (
            {
                key: value
                for key, value in chs.items()
                if key not in {"wav_names", "lab_names"}
            }
            if chs
            else None
        ),
        "reference": (
            {
                key: value
                for key, value in reference.items()
                if key not in {"wav_names", "lab_names"}
            }
            if reference
            else None
        ),
        "character": character,
        "index": selected_index,
        "index_candidates": indexes[:8],
        "remote_index_attempt": remote_attempt,
        "translation": {
            "provider": api["provider"],
            "base_url": api["base_url"],
            "configured": api["configured"],
            "model": DEFAULT_MODEL,
            "source_text_language": source_text_language,
            "remote_index_url": remote_index_url,
            "official_chinese_matches": official_chinese_matches,
            "pending_translation_count": len(pending_records),
            **translation_estimate,
        },
        "blockers": blockers,
        "warnings": warnings,
        "ready": not blockers,
    }


def _safe_project_name(plan: dict[str, Any], source: Path) -> str:
    char = str(plan.get("character", {}).get("value", "")).strip()
    return char or source.stem or "voice-archive"


def _safe_project_dir_name(value: str) -> str:
    # Keep readable Unicode project names while removing characters that are
    # unsafe on common Android/Windows filesystems.
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", str(value or "").strip())
    stem = re.sub(r"\s+", " ", stem).strip(" .-")
    stem = stem[:96].rstrip(" .-")
    return stem or "voice-archive"


def _default_project_base() -> Path:
    override = os.environ.get("HSR_VOICE_PROJECTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()

    # Quick Mode on Termux should place the whole project in shared storage so
    # the generated archive is visible to Android file managers. The output
    # remains the project's "output" subdirectory, avoiding collisions between
    # characters/projects.
    if os.environ.get("TERMUX_VERSION"):
        home = Path.home()
        for downloads in (
            home / "storage" / "downloads",
            home / "storage" / "shared" / "Download",
            Path("/storage/emulated/0/Download"),
        ):
            try:
                if downloads.is_dir() and os.access(downloads, os.W_OK):
                    return downloads / "HSR_Voice_Test"
            except OSError:
                continue

    return Path.home() / "HSR-Voice-Projects"


def _default_project_root(plan: dict[str, Any], source: Path) -> Path:
    base = _default_project_base()
    stem = _safe_project_dir_name(_safe_project_name(plan, source))
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
    reference_source: Path | None = None,
    root: Path | None = None,
    name: str = "",
    audio_language: str = "auto",
    source_text_language: str = "en",
    target_language: str = "zh-CN",
    reference_language: str = "auto",
) -> tuple[ProjectConfig, dict[str, Any]]:
    english_source = english_source.expanduser().resolve()
    chs_source = chs_source.expanduser().resolve() if chs_source else None
    reference_source = reference_source.expanduser().resolve() if reference_source else None
    plan = quick_scan(
        english_source,
        chs_source,
        reference_source=reference_source,
        source_text_language=source_text_language,
    )
    if not plan["ready"]:
        raise RuntimeError("Quick scan has blockers: " + "; ".join(plan["blockers"]))

    selected_index = plan["index"]
    assert selected_index is not None
    current_inventory = source_inventory(english_source)
    if current_inventory["fingerprint"] != plan["english"]["fingerprint"]:
        raise RuntimeError("English source changed after Quick Scan; scan again before building")
    if chs_source is not None:
        current_chs = source_inventory(chs_source)
        if current_chs["fingerprint"] != plan["chinese"]["fingerprint"]:
            raise RuntimeError("Chinese source changed after Quick Scan; scan again before building")
    wanted = set(current_inventory["wav_names"])
    if selected_index.get("source") == "remote":
        remote_records, _ = fetch_ai_hobbyist_index_for_filenames_cached(
            wanted,
            str(selected_index["url"]),
        )
        if _index_fingerprint(remote_records) != selected_index["records_fingerprint"]:
            raise RuntimeError("Remote index changed after Quick Scan; scan again before building")
        filtered = _remote_rows(remote_records, wanted)
    else:
        local_index = Path(selected_index["path"])
        if sha256_file(local_index) != selected_index["file_sha256"]:
            raise RuntimeError("Local index changed after Quick Scan; scan again before building")
        original_rows = normalize_index(local_index)
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
        reference_source=str(reference_source) if reference_source else "",
        audio_language=audio_language,
        source_text_language=source_text_language,
        target_language=target_language,
        reference_language=reference_language,
        remote_character=str(plan.get("character", {}).get("value", "")),
    )
    update_project(
        config,
        make_flac=True,
        translate_missing=bool(plan["translation"]["configured"]),
        translation_model=DEFAULT_MODEL,
        remote_index_url=str(selected_index.get("url", ai_hobbyist_index_url(source_text_language))),
    )

    atomic_write_text(
        project_root / ".generated" / "quick_scan.json",
        json.dumps(plan, ensure_ascii=False, indent=2),
    )
    return config, plan
