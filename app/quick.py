from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from .builder import (
    MAX_ARCHIVE_MEMBERS,
    atomic_write_text,
    cross_language_voice_key,
    map_labs_to_voice_filenames,
    sha256_file,
)
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
# Partial index coverage is allowed, but below this share the scan still
# blocks: the continuous archive would be ordered mostly by guesswork.
MIN_INDEX_COVERAGE = 0.5

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

    wav_members = [
        str(item["name"]) for item in files if str(item["name"]).lower().endswith(".wav")
    ]
    lab_members = [
        str(item["name"]) for item in files if str(item["name"]).lower().endswith(".lab")
    ]
    wavs = [Path(name).name for name in wav_members]
    labs = [Path(name).stem for name in lab_members]
    wav_stems = {Path(name).stem for name in wavs}
    lab_stems = set(labs)
    members_by_basename: dict[str, list[str]] = {}
    for member in wav_members:
        members_by_basename.setdefault(Path(member).name, []).append(member)
    duplicate_groups = {
        name: members for name, members in members_by_basename.items() if len(members) > 1
    }
    duplicate_wavs = sorted(duplicate_groups)

    # Member-level LAB pairing: a WAV counts as paired when its own sibling
    # LAB (same directory, same stem) exists, or when its stem is unique
    # across the package so the single LAB with that stem is unambiguous.
    lab_member_names = {str(name).casefold() for name in lab_members}
    lab_stem_counts = Counter(Path(name).stem for name in lab_members)
    member_pairs = 0
    for member in wav_members:
        sibling = PurePosixPath(member).with_suffix(".lab").as_posix()
        if sibling.casefold() in lab_member_names:
            member_pairs += 1
        elif lab_stem_counts.get(Path(member).stem, 0) == 1:
            member_pairs += 1

    return {
        "source": str(source),
        "kind": "directory" if source.is_dir() else source.suffix.lower().lstrip("."),
        "file_count": len(files),
        "wav_count": len(wavs),
        "lab_count": len(labs),
        "wav_names": wavs,
        "lab_names": labs,
        "wav_members": wav_members,
        "lab_members": lab_members,
        "duplicate_wav_names": duplicate_wavs,
        "duplicate_wav_groups": duplicate_groups,
        "wav_lab_pairs": len(wav_stems & lab_stems),
        "wav_lab_member_pairs": member_pairs,
        "wav_without_lab": len(wav_stems - lab_stems),
        "declared_bytes": sum(int(item.get("size", 0)) for item in files),
        "fingerprint": {
            "algorithm": "tree-sha256-v1" if source.is_dir() else "sha256",
            "digest": content_hash.hexdigest(),
            "source_size_bytes": source_stat.st_size,
            "source_modified_ns": source_stat.st_mtime_ns,
        },
    }


def _natural_member_key(member: str) -> tuple[Any, ...]:
    """Natural-sort key so chapter2 sorts before chapter10 in member paths."""
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", str(member))
    )


def _extract_source_members(
    source: Path,
    members: list[str],
    dest: Path,
) -> dict[str, Path]:
    """Materialize selected package members under dest.

    Returns a mapping keyed by the requested member names. Lookup is
    case-insensitive so a computed sibling name (``x.lab``) still finds a
    package member stored as ``x.LAB``. Only the requested members are
    decompressed, which keeps duplicate-group inspection cheap even for
    large archives.
    """
    source = Path(source)
    requested = {str(member).casefold(): str(member) for member in members}
    out: dict[str, Path] = {}
    if source.is_dir():
        for member in members:
            candidate = source / str(member)
            if candidate.is_file():
                target = dest / str(member)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, target)
                out[str(member)] = target
        return out
    suffix = source.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(source) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = _safe_member_name(info.filename)
                canonical = requested.get(name.casefold())
                if canonical is None or canonical in out:
                    continue
                target = dest / canonical
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as fin, target.open("wb") as fout:
                    shutil.copyfileobj(fin, fout)
                out[canonical] = target
        return out
    if suffix == ".7z":
        exe = shutil.which("7zz") or shutil.which("7z")
        if not exe:
            raise RuntimeError("7-Zip CLI is required to inspect .7z packages")
        dest.mkdir(parents=True, exist_ok=True)
        # Windows command lines cap at ~32k characters, so extract in chunks.
        chunk_size = 200
        member_list = [str(member) for member in members]
        for start in range(0, len(member_list), chunk_size):
            chunk = member_list[start : start + chunk_size]
            extracted = subprocess.run(
                [exe, "x", "-y", f"-o{dest}", str(source), *chunk],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                check=False,
            )
            if extracted.returncode != 0:
                raise RuntimeError(
                    f"7-Zip member extraction failed ({extracted.returncode}): "
                    f"{extracted.stderr.strip()}"
                )
        extracted_files: dict[str, Path] = {}
        for path in dest.rglob("*"):
            if path.is_file():
                extracted_files[path.relative_to(dest).as_posix().casefold()] = path
        for folded, canonical in requested.items():
            path = extracted_files.get(folded)
            if path is not None:
                out[canonical] = path
        return out
    raise ValueError("Quick mode accepts a directory, .zip, or .7z source")


def _hash_source_members(source: Path, members: list[str]) -> dict[str, str]:
    """SHA-256 the given package members without extracting the whole package."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="hsr-members-") as td:
        paths = _extract_source_members(Path(source), members, Path(td))
        return {member: sha256_file(path) for member, path in paths.items()}


def _member_lab_texts(source: Path, wav_members: list[str]) -> dict[str, str]:
    """Read the sibling (same-directory same-stem) LAB text for WAV members."""
    sibling_of = {
        str(member): PurePosixPath(str(member)).with_suffix(".lab").as_posix()
        for member in wav_members
    }
    wanted = sorted(set(sibling_of.values()))
    if not wanted:
        return {}
    import tempfile

    with tempfile.TemporaryDirectory(prefix="hsr-labs-") as td:
        paths = _extract_source_members(Path(source), wanted, Path(td))
        texts: dict[str, str] = {}
        for member, sibling in sibling_of.items():
            path = paths.get(sibling)
            if path is None:
                continue
            texts[member] = path.read_text(
                encoding="utf-8-sig", errors="replace"
            ).strip()
        return texts


def _disambiguate_member(
    source: Path,
    members: list[str],
    row: dict[str, str],
) -> str:
    """Pick the one duplicate-basename member an index row really describes.

    A valid index SHA-256 is decisive. Otherwise exact LAB-text equality may
    identify the member. Anything inconclusive returns "" so the caller
    demotes the whole group to the appendix instead of guessing.
    """
    expected_hash = str(row.get("sha256", "")).strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        hashes = _hash_source_members(source, members)
        winners = [
            member
            for member in members
            if hashes.get(member, "").lower() == expected_hash
        ]
        return winners[0] if len(winners) == 1 else ""
    expected_text = str(row.get("english", "")).strip()
    if expected_text:
        texts = _member_lab_texts(source, members)
        winners = [
            member for member in members if texts.get(member, "") == expected_text
        ]
        return winners[0] if len(winners) == 1 else ""
    return ""


def _package_lab_rows(
    source: Path,
    inventory: dict[str, Any],
) -> list[dict[str, str]]:
    """Synthesize index rows from a package whose WAVs all have LAB text.

    Ordering follows the package's own directory structure (natural member
    path order), which matches chapter/mission layout in practice. Each row
    carries its source_member_id so same-basename members stay distinct.
    """
    members = sorted(
        (str(m) for m in inventory.get("wav_members", [])),
        key=_natural_member_key,
    )
    lab_texts = _member_lab_texts(source, members)
    rows: list[dict[str, str]] = []
    for pos, member in enumerate(members, 1):
        filename = Path(member).name
        rows.append({
            "index": str(pos),
            "group": parse_voice_identity(filename).group,
            "filename": filename,
            "source": "primary-package-lab",
            "source_detail": "same-stem LAB",
            "english": lab_texts.get(member, ""),
            "reference_text": "",
            "reference_language": "",
            "official_target_text": "",
            "official_target_language": "",
            "official_target_source": "",
            "sha256": "",
            "source_member_id": member,
        })
    return rows


def _package_lab_candidate(
    inventory: dict[str, Any],
    source_text_language: str,
) -> dict[str, Any]:
    """Synthetic scan candidate: the package itself is the index."""
    wav_count = int(inventory.get("wav_count", 0))
    return {
        "source": "primary-package-lab",
        "provider": "primary package LAB",
        "source_text_language": source_text_language,
        "url": "",
        "row_count": wav_count,
        "matched_wavs": wav_count,
        "english_matched": int(inventory.get("wav_lab_member_pairs", 0)),
        "coverage": 1.0,
        "duplicate_filenames": [],
        "characters": [],
        "character_counts": {},
        "primary_character": "",
        "primary_character_share": 0.0,
        "order_basis": "package_member_path",
        "records_fingerprint": "",
        "cache": {"cache_hit": False, "stale": False},
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


def _remote_rows(
    records: list[dict[str, str]],
    wanted: set[str],
    provider_label: str = "AI-Hobbyist EN.xlsx",
) -> tuple[list[dict[str, str]], list[str]]:
    """Build index rows for wanted filenames, tolerating partial coverage.

    Filenames that match several conflicting index rows are ambiguous and are
    reported instead of raising, so the rest of the package can still build.
    Returns (rows, ambiguous_names).
    """
    result_by_name: dict[str, dict[str, str]] = {}
    ambiguous: set[str] = set()
    for pos, row in enumerate(records, 1):
        filename = Path(str(row.get("filename", ""))).name
        if filename not in wanted or filename in ambiguous:
            continue
        if filename in result_by_name:
            # Conflicting duplicates make every occurrence unreliable, so the
            # file is demoted to the unindexed appendix entirely.
            ambiguous.add(filename)
            del result_by_name[filename]
            continue
        remote_hash = str(row.get("hash", "")).strip().lower()
        result_by_name[filename] = {
            "index": str(pos),
            "group": parse_voice_identity(filename).group,
            "filename": filename,
            "source": provider_label,
            "source_detail": str(row.get("character", "")).strip(),
            "english": str(row.get("english", "")).strip(),
            "sha256": remote_hash if re.fullmatch(r"[0-9a-f]{64}", remote_hash) else "",
        }
    return list(result_by_name.values()), sorted(ambiguous)


def _cross_language_voice_key(filename: str) -> str:
    return cross_language_voice_key(filename)


def _map_reference_records(
    primary_names: set[str],
    records: list[dict[str, str]],
) -> tuple[dict[str, str], dict[str, int]]:
    """Map a second localization's indexed text onto primary voice filenames.

    Exact filenames win. For character-localized filenames, a conservative
    group+numeric-tail key can bridge names such as
    chapter5_13_evanescia_103.wav and chapter5_13_<localized-name>_103.wav.
    Structural fallback is used only when the key is unique on both sides.
    """
    by_exact = {
        Path(str(row.get("filename", ""))).name: str(row.get("english", "")).strip()
        for row in records
        if str(row.get("filename", "")).strip() and str(row.get("english", "")).strip()
    }
    mapped: dict[str, str] = {}
    exact = 0
    for name in primary_names:
        text = by_exact.get(Path(name).name, "")
        if text:
            mapped[name] = text
            exact += 1

    remaining_primary = [name for name in primary_names if name not in mapped]
    primary_by_key: dict[str, list[str]] = {}
    for name in remaining_primary:
        primary_by_key.setdefault(_cross_language_voice_key(name), []).append(name)

    reference_by_key: dict[str, list[tuple[str, str]]] = {}
    for row in records:
        filename = Path(str(row.get("filename", ""))).name
        text = str(row.get("english", "")).strip()
        if not filename or not text:
            continue
        reference_by_key.setdefault(
            _cross_language_voice_key(filename), []
        ).append((filename, text))

    structural = 0
    for key, names in primary_by_key.items():
        refs = reference_by_key.get(key, [])
        if len(names) == 1 and len(refs) == 1:
            mapped[names[0]] = refs[0][1]
            structural += 1

    return mapped, {
        "exact": exact,
        "structural": structural,
        "total": len(mapped),
    }


def quick_scan(
    english_source: Path,
    chs_source: Path | None = None,
    *,
    reference_source: Path | None = None,
    source_text_language: str = "en",
    target_language: str = "zh-CN",
    reference_language: str = "auto",
    remote_index_url: str = "",
) -> dict[str, Any]:
    english = source_inventory(english_source)
    blockers: list[str] = []
    warnings: list[str] = []

    # Member-level LAB coverage: every WAV member has its own sibling LAB or
    # a tree-wide unique same-stem LAB. When complete, the package itself can
    # act as the index (ordering from member paths, text from LABs), so an
    # external index becomes optional for any source language.
    complete_primary_lab = (
        int(english.get("wav_count", 0)) > 0
        and int(english.get("wav_lab_member_pairs", 0))
        == int(english.get("wav_count", 0))
    )

    remote_index_url = remote_index_url.strip()
    if not remote_index_url:
        try:
            remote_index_url = ai_hobbyist_index_url(source_text_language)
        except ValueError:
            # AI-Hobbyist currently publishes EN/CHS/JP/KR indexes. Other
            # source languages can still use EN only for ordering when the
            # primary audio package supplies complete same-stem LAB text.
            if complete_primary_lab:
                remote_index_url = DEFAULT_EN_INDEX_URL
                if source_text_language != "en":
                    warnings.append(
                        f"No built-in remote {source_text_language} index; using EN.xlsx "
                        "only for ordering while source text comes from primary-package LAB files"
                    )
            else:
                remote_index_url = DEFAULT_EN_INDEX_URL
                blockers.append(
                    f"No built-in remote index for source language {source_text_language}; "
                    "provide same-stem LAB text for every WAV or choose a supported source "
                    "language (en, zh-CN, ja, ko)"
                )

    if english["wav_count"] == 0:
        blockers.append("No WAV files were found in the primary audio source")
    wav_names = {Path(name).name for name in english["wav_names"]}
    # Existing local CSV discovery predates explicit language roles and its
    # schema does not declare the text language. Keep that legacy shortcut only
    # for English; non-English Quick Mode uses the selected CHS/JP/KR index
    # instead of silently treating an arbitrary local CSV as the requested language.
    indexes = (
        _index_candidates(Path(english["source"]), wav_names)
        if source_text_language == "en"
        else []
    )
    selected_index = indexes[0] if indexes else None
    if selected_index is not None:
        selected_index = {**selected_index, "source": "local"}

    character = infer_character(list(wav_names))
    total_wavs = int(english["wav_count"])

    def _index_coverage_key(index: dict[str, Any]) -> tuple[int, int]:
        return (int(index.get("matched_wavs", 0)), int(index.get("english_matched", 0)))

    # Coverage is counted against unique basenames; same-basename duplicates
    # share one index row and are resolved per member later.
    local_full = bool(
        selected_index
        and int(selected_index["matched_wavs"]) == len(wav_names)
        and int(selected_index["english_matched"]) == len(wav_names)
    )
    remote_attempt: dict[str, Any] | None = None
    remote_records: list[dict[str, str]] = []
    if not local_full and not blockers:
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
            # Per-file text is exact even for partial coverage, so keep
            # whichever candidate carries more information instead of
            # requiring a complete index up front.
            if int(remote_attempt["matched_wavs"]) > 0 and (
                selected_index is None
                or _index_coverage_key(remote_attempt) > _index_coverage_key(selected_index)
            ):
                selected_index = remote_attempt
        except Exception as exc:
            warnings.append(f"Remote index fallback failed: {type(exc).__name__}: {exc}")

    index_usable = False
    package_lab_index = False
    if selected_index is not None and int(selected_index.get("matched_wavs", 0)) == 0:
        selected_index = None
    if selected_index is None:
        if complete_primary_lab:
            selected_index = _package_lab_candidate(english, source_text_language)
            index_usable = True
            package_lab_index = True
            warnings.append(
                "未找到覆盖该语音包的可靠索引；包内每个 WAV 都有同名 LAB 文本，"
                "已按包内目录顺序排列并使用 LAB 作为源文本"
            )
        else:
            blockers.append("No reliable local or remote index covers the package WAV names")
    else:
        matched_wavs = int(selected_index["matched_wavs"])
        text_matched = int(selected_index["english_matched"])
        coverage = matched_wavs / max(1, total_wavs)
        if coverage < MIN_INDEX_COVERAGE:
            if complete_primary_lab:
                selected_index = _package_lab_candidate(english, source_text_language)
                index_usable = True
                package_lab_index = True
                warnings.append(
                    f"最佳索引仅覆盖 {matched_wavs} / {total_wavs} 个 WAV（{coverage:.0%}），"
                    f"低于 {MIN_INDEX_COVERAGE:.0%} 下限；包内 LAB 文本完整，"
                    "已改用包内目录顺序与同名 LAB 源文本"
                )
            else:
                blockers.append(
                    f"Best index covers only {matched_wavs} / {total_wavs} WAV files "
                    f"({coverage:.0%}), below the {MIN_INDEX_COVERAGE:.0%} minimum; check "
                    "that this is the right character package or provide a more "
                    "complete index"
                )
        else:
            index_usable = True
            uncovered = total_wavs - matched_wavs
            if uncovered:
                warnings.append(
                    f"Index has no entry for {uncovered} / {total_wavs} WAV files; "
                    "those files keep their audio but are appended at the end "
                    "without indexed order or subtitles"
                )
            if text_matched < matched_wavs:
                warnings.append(
                    f"Index has no source text for {matched_wavs - text_matched} "
                    "matched WAV files; they are treated like uncovered files"
                )
            if selected_index.get("duplicate_filenames"):
                warnings.append(
                    f"{len(selected_index['duplicate_filenames'])} filenames match "
                    "multiple index rows and are treated as uncovered: "
                    + ", ".join(selected_index["duplicate_filenames"][:5])
                )
            if (
                selected_index.get("source") == "remote"
                and float(selected_index.get("primary_character_share", 0.0)) < 0.75
            ):
                warnings.append(
                    "Matched index rows are not dominated by one character label "
                    f"(primary: {selected_index.get('primary_character', '')} "
                    f"{float(selected_index.get('primary_character_share', 0.0)):.0%}); "
                    "per-file text is still exact, but review the package if this "
                    "should be a single-character archive"
                )

    chs = None
    if chs_source is not None and str(chs_source).strip():
        if Path(chs_source).expanduser().resolve() == Path(english["source"]).resolve():
            warnings.append(
                "Primary audio and official Chinese voice package are the same; "
                "the second selection is unnecessary when the primary source text is Chinese"
            )
        chs = source_inventory(chs_source)
        if chs["lab_count"] == 0:
            warnings.append("Official Chinese voice package contains no LAB files")

    reference = None
    reference_index_attempt: dict[str, Any] | None = None
    reference_text_match = {"exact": 0, "structural": 0, "total": 0}
    if reference_source is not None and str(reference_source).strip():
        reference = source_inventory(reference_source)
        primary_stems = {Path(name).stem for name in wav_names}
        reference_lab_stems = set(reference.get("lab_names", []))
        lab_matches = len(primary_stems & reference_lab_stems)
        if reference["lab_count"] > 0:
            reference_text_match = {
                "exact": lab_matches,
                "structural": 0,
                "total": lab_matches,
            }
            if lab_matches < len(wav_names):
                warnings.append(
                    f"Reference LAB matches only {lab_matches} / {len(wav_names)} primary voices"
                )
        elif reference_language == "auto":
            warnings.append(
                "Reference package has no LAB text. Choose its language (EN/CHS/JP/KR) "
                "so Quick Mode can recover reference text from the matching remote index."
            )
        else:
            try:
                reference_index_url = ai_hobbyist_index_url(reference_language)
                reference_wavs = {Path(name).name for name in reference["wav_names"]}
                reference_records, reference_cache = (
                    fetch_ai_hobbyist_index_for_filenames_cached(
                        reference_wavs, reference_index_url
                    )
                )
                reference_index_attempt = _remote_candidate(
                    reference_records,
                    reference_wavs,
                    url=reference_index_url,
                    cache=reference_cache,
                    source_text_language=reference_language,
                )
                mapped_reference, reference_text_match = _map_reference_records(
                    wav_names, reference_records
                )
                reference_index_attempt["primary_text_matches"] = len(mapped_reference)
                reference_index_attempt["match_detail"] = reference_text_match
                reference_index_attempt["records_fingerprint"] = _index_fingerprint(
                    reference_records
                )
                if reference_cache.get("stale"):
                    warnings.append(
                        "Reference index refresh failed; a stale cached copy was used"
                    )
                if not mapped_reference:
                    warnings.append(
                        "Reference package index was found, but no voice lines could be "
                        "aligned with the primary package"
                    )
                elif len(mapped_reference) < len(wav_names):
                    warnings.append(
                        f"Reference text aligns with {len(mapped_reference)} / "
                        f"{len(wav_names)} primary voices"
                    )
            except Exception as exc:
                warnings.append(
                    f"Reference text index fallback failed: {type(exc).__name__}: {exc}"
                )

    if character["confidence"] == "low":
        warnings.append("Character inference confidence is low; review before building")

    api = credentials_status()
    if not api["configured"]:
        warnings.append("AI translation API is not configured; unmatched target text will remain missing")

    pending_records: list[dict[str, str]] = []
    official_chinese_matches = 0
    official_chinese_exact = 0
    official_chinese_structural = 0
    official_chinese_unmatched = 0
    official_chinese_conflicts = 0
    index_rows: list[dict[str, str]] = []
    if index_usable and selected_index is not None:
        if selected_index.get("source") == "remote":
            index_rows, _ambiguous_rows = _remote_rows(
                remote_records,
                wav_names,
                str(selected_index.get("provider") or ai_hobbyist_index_label(remote_index_url)),
            )
        elif selected_index.get("source") == "primary-package-lab":
            index_rows = _package_lab_rows(Path(english["source"]), english)
        else:
            index_rows = [
                row for row in normalize_index(Path(selected_index["path"]))
                if Path(str(row.get("filename", ""))).name in wav_names
            ]
        chinese_stems = set(chs.get("lab_names", [])) if chs else set()
        if source_text_language == target_language == "zh-CN":
            chinese_stems.update(english.get("lab_names", []))
        official_map, match_detail = map_labs_to_voice_filenames(
            [str(row.get("filename", "")) for row in index_rows],
            {stem: "present" for stem in chinese_stems},
        )
        official_chinese_matches = match_detail["total"]
        official_chinese_exact = match_detail["exact"]
        official_chinese_structural = match_detail["structural"]
        official_chinese_unmatched = match_detail.get("unmatched", len(index_rows) - official_chinese_matches)
        official_chinese_conflicts = match_detail.get("conflicts", 0)

        pending_records = [
            {
                "id": Path(str(row.get("filename", ""))).name,
                "english": str(row.get("english", "")).strip(),
            }
            for row in index_rows
            if Path(str(row.get("filename", ""))).name not in official_map
            and str(row.get("english", "")).strip()
        ]
        if chs_source is not None and official_chinese_conflicts > 0:
            blockers.append(
                f"Official Chinese package has {official_chinese_conflicts} matching conflicts/collisions; "
                "API fallback is blocked to prevent ambiguous subtitle assignment"
            )
        elif chs_source is not None and chinese_stems and not official_chinese_matches:
            blockers.append(
                "Official Chinese package has LAB files but none can be safely matched "
                "to the primary voices; API fallback is blocked to prevent an unintended full translation"
            )
        elif chs_source is not None and official_chinese_matches < len(index_rows):
            warnings.append(
                f"Official Chinese subtitles match {official_chinese_matches} / {len(index_rows)} "
                f"voices ({official_chinese_exact} exact, {official_chinese_structural} cross-language, "
                f"{official_chinese_unmatched} unmatched); "
                "only unmatched items may use AI translation"
            )
    # Same-basename WAV groups no longer block the build. When an index row
    # exists for the basename we try to bind it to exactly one package member
    # (audio hash first, then LAB text); members that cannot be bound
    # reliably are demoted to the appendix instead of failing the scan.
    duplicate_groups = english.get("duplicate_wav_groups") or {}
    duplicate_report: dict[str, Any] = {
        "groups": len(duplicate_groups),
        "members": sum(len(members) for members in duplicate_groups.values()),
        "auto_resolved": 0,
        "pending": 0,
        "pending_members": [],
    }
    if duplicate_groups:
        if package_lab_index:
            # Every member becomes its own synthesized row, so nothing in a
            # duplicate group is ambiguous.
            duplicate_report["auto_resolved"] = duplicate_report["members"]
        else:
            row_by_basename = {
                Path(str(row.get("filename", ""))).name: row for row in index_rows
            }
            for basename in sorted(duplicate_groups):
                members = list(duplicate_groups[basename])
                winner = ""
                row = row_by_basename.get(basename)
                if row is not None:
                    try:
                        winner = _disambiguate_member(
                            Path(english["source"]), members, row
                        )
                    except Exception as exc:
                        warnings.append(
                            f"同名 WAV 自动消歧失败（{basename}）："
                            f"{type(exc).__name__}: {exc}"
                        )
                if winner:
                    duplicate_report["auto_resolved"] += 1
                    losers = [m for m in members if m != winner]
                else:
                    losers = members
                duplicate_report["pending"] += len(losers)
                duplicate_report["pending_members"].extend(losers)
        if duplicate_report["pending"]:
            warnings.append(
                f"发现 {duplicate_report['groups']} 组同名 WAV"
                f"（共 {duplicate_report['members']} 个文件）："
                f"已自动消歧 {duplicate_report['auto_resolved']} 条 / "
                f"待确认 {duplicate_report['pending']} 条；"
                "待确认文件保留音频并列入附录，不阻塞构建"
            )
        else:
            warnings.append(
                f"发现 {duplicate_report['groups']} 组同名 WAV"
                f"（共 {duplicate_report['members']} 个文件），已全部自动消歧"
            )

    translation_estimate = estimate_workload_tokens(pending_records, 80)

    return {
        "schema_version": 2,
        "kind": "quick_scan",
        "source_text_language": source_text_language,
        "english": {
            key: value
            for key, value in english.items()
            if key not in {"wav_names", "lab_names", "wav_members", "lab_members"}
        },
        "chinese": (
            {
                key: value
                for key, value in chs.items()
                if key not in {"wav_names", "lab_names", "wav_members", "lab_members"}
            }
            if chs
            else None
        ),
        "reference_index_attempt": reference_index_attempt,
        "reference_text_match": reference_text_match,
        "reference": (
            {
                key: value
                for key, value in reference.items()
                if key not in {"wav_names", "lab_names", "wav_members", "lab_members"}
            }
            if reference
            else None
        ),
        "duplicates": duplicate_report,
        "character": character,
        "index": selected_index,
        "index_coverage": {
            "matched_wavs": (
                int(selected_index["matched_wavs"]) if selected_index is not None else 0
            ),
            "text_matched_wavs": (
                int(selected_index["english_matched"]) if selected_index is not None else 0
            ),
            "total_wavs": total_wavs,
            "uncovered_wavs": (
                total_wavs - int(selected_index["matched_wavs"])
                if selected_index is not None
                else total_wavs
            ),
        },
        "index_candidates": indexes[:8],
        "remote_index_attempt": remote_attempt,
        "translation": {
            "provider": api["provider"],
            "base_url": api["base_url"],
            "configured": api["configured"],
            "model": DEFAULT_MODEL,
            "source_text_language": source_text_language,
            "target_language": target_language,
            "remote_index_url": remote_index_url,
            "official_chinese_matches": official_chinese_matches,
            "official_chinese_exact": official_chinese_exact,
            "official_chinese_structural": official_chinese_structural,
            "official_chinese_unmatched": official_chinese_unmatched,
            "official_chinese_conflicts": official_chinese_conflicts,
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
        target_language=target_language,
        reference_language=reference_language,
    )
    if not plan["ready"]:
        raise RuntimeError("Quick scan has blockers: " + "; ".join(plan["blockers"]))

    selected_index = plan["index"]
    assert selected_index is not None
    current_inventory = source_inventory(english_source)
    if current_inventory["fingerprint"] != plan["english"]["fingerprint"]:
        raise RuntimeError("Primary audio source changed after Quick Scan; scan again before building")
    current_chs = None
    if chs_source is not None:
        current_chs = source_inventory(chs_source)
        if current_chs["fingerprint"] != plan["chinese"]["fingerprint"]:
            raise RuntimeError("Target-text source changed after Quick Scan; scan again before building")
    current_reference = None
    if reference_source is not None:
        current_reference = source_inventory(reference_source)
        if current_reference["fingerprint"] != plan["reference"]["fingerprint"]:
            raise RuntimeError("Reference source changed after Quick Scan; scan again before building")
    wanted_members = [str(m) for m in current_inventory.get("wav_members", [])]
    if not wanted_members:
        # Defensive fallback; source_inventory always provides member paths.
        wanted_members = sorted(set(current_inventory["wav_names"]))
    members_by_basename: dict[str, list[str]] = {}
    for member in wanted_members:
        members_by_basename.setdefault(Path(member).name, []).append(member)
    wanted = set(members_by_basename)

    package_lab_index = selected_index.get("source") == "primary-package-lab"
    base_rows: list[dict[str, str]] = []
    if package_lab_index:
        base_rows = _package_lab_rows(english_source, current_inventory)
    elif selected_index.get("source") == "remote":
        remote_records, _ = fetch_ai_hobbyist_index_for_filenames_cached(
            wanted,
            str(selected_index["url"]),
        )
        if _index_fingerprint(remote_records) != selected_index["records_fingerprint"]:
            raise RuntimeError("Remote index changed after Quick Scan; scan again before building")
        base_rows, _ambiguous = _remote_rows(
            remote_records,
            wanted,
            str(selected_index.get("provider") or ai_hobbyist_index_label(str(selected_index["url"]))),
        )
    else:
        local_index = Path(selected_index["path"])
        if sha256_file(local_index) != selected_index["file_sha256"]:
            raise RuntimeError("Local index changed after Quick Scan; scan again before building")
        original_rows = normalize_index(local_index)
        base_rows = [row for row in original_rows if Path(row["filename"]).name in wanted]

    # Bind each index row to exactly one package member. Same-basename groups
    # are resolved by hash/LAB disambiguation; anything undecided falls
    # through to the appendix below instead of blocking the build.
    assigned: dict[str, dict[str, str]] = {}
    used_indexes: set[int] = set()
    for row in base_rows:
        basename = Path(str(row.get("filename", ""))).name
        candidates = [
            member for member in members_by_basename.get(basename, [])
            if member not in assigned
        ]
        if not candidates:
            continue
        if len(candidates) == 1:
            member = candidates[0]
        else:
            member = _disambiguate_member(english_source, candidates, row)
        if not member:
            continue
        bound = dict(row)
        bound["filename"] = basename
        bound["source_member_id"] = member
        assigned[member] = bound
        try:
            used_indexes.add(int(str(bound.get("index", "")).strip()))
        except ValueError:
            pass

    filtered = list(assigned.values())
    # Members the index cannot order reliably keep their audio but join the
    # archive as an unindexed appendix at the end, without subtitles. This
    # includes the undecided members of same-basename duplicate groups.
    uncovered_members = [
        member
        for member in sorted(wanted_members, key=_natural_member_key)
        if member not in assigned
    ]
    next_index = (max(used_indexes) + 1) if used_indexes else 1
    for member in uncovered_members:
        basename = Path(member).name
        filtered.append({
            "index": str(next_index),
            "group": parse_voice_identity(basename).group,
            "filename": basename,
            "source": "unindexed-package-file",
            "source_detail": "",
            "english": "",
            "reference_text": "",
            "reference_language": "",
            "official_target_text": "",
            "official_target_language": "",
            "official_target_source": "",
            "sha256": "",
            "source_member_id": member,
        })
        next_index += 1
    if len(filtered) != len(wanted_members):
        raise RuntimeError("Index coverage changed between scan and project creation")

    reference_text_embedded = False
    reference_attempt = plan.get("reference_index_attempt")
    if (
        reference_source is not None
        and current_reference is not None
        and int(current_reference.get("lab_count", 0)) == 0
        and isinstance(reference_attempt, dict)
        and reference_attempt.get("url")
    ):
        reference_wavs = {
            Path(name).name for name in current_reference.get("wav_names", [])
        }
        reference_records, _ = fetch_ai_hobbyist_index_for_filenames_cached(
            reference_wavs,
            str(reference_attempt["url"]),
        )
        if _index_fingerprint(reference_records) != reference_attempt.get(
            "records_fingerprint"
        ):
            raise RuntimeError(
                "Reference remote index changed after Quick Scan; scan again before building"
            )
        mapped_reference, _ = _map_reference_records(wanted, reference_records)
        for row in filtered:
            filename = Path(str(row.get("filename", ""))).name
            text = mapped_reference.get(filename, "")
            if text:
                row["reference_text"] = text
                row["reference_language"] = reference_language
        reference_text_embedded = bool(mapped_reference)

    project_root = (root or _default_project_root(plan, english_source)).expanduser().resolve()
    generated_dir = project_root / ".generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    generated_index = generated_dir / "quick_index.csv"

    fields = [
        "index", "group", "filename", "source", "source_detail", "english",
        "reference_text", "reference_language",
        "official_target_text", "official_target_language", "official_target_source",
        "sha256", "source_member_id",
    ]
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
        wav_source_fingerprint=str(current_inventory.get("fingerprint", {}).get("digest", "") or ""),
        chs_source_fingerprint=str(
            (current_chs or {}).get("fingerprint", {}).get("digest", "") or ""
        ),
        reference_source_fingerprint=str(
            (current_reference or {}).get("fingerprint", {}).get("digest", "") or ""
        ),
        reference_text_embedded=reference_text_embedded,
        managed_project_root=True,
        audio_language=audio_language,
        source_text_language=source_text_language,
        target_language=target_language,
        reference_language=reference_language,
        remote_character=str(
            selected_index.get("primary_character")
            or plan.get("character", {}).get("value", "")
        ),
    )
    update_project(
        config,
        make_flac=True,
        generate_ass=False,
        translate_missing=bool(plan["translation"]["configured"]),
        review_official_target=False,
        translation_model=DEFAULT_MODEL,
        remote_index_url=str(
            selected_index.get("url")
            or plan.get("translation", {}).get("remote_index_url")
            or DEFAULT_EN_INDEX_URL
        ),
    )

    atomic_write_text(
        project_root / ".generated" / "quick_scan.json",
        json.dumps(plan, ensure_ascii=False, indent=2),
    )
    return config, plan


def remote_character_candidates(
    config: ProjectConfig,
    requested: str = "",
) -> list[str]:
    """Return remote role labels in the order an update check should try.

    Quick Mode can infer an ASCII token from WAV filenames, such as
    "evanescia", while the remote workbook stores a localized role label,
    such as "绯英". The saved Quick Scan result is therefore a more reliable
    source for the remote role filter than the filename token.
    """

    saved = str(config.remote_character or "").strip()
    detected = ""
    scan_path = (
        Path(config.root).expanduser().resolve()
        / ".generated"
        / "quick_scan.json"
    )
    try:
        payload = json.loads(scan_path.read_text(encoding="utf-8"))
        sections = (payload.get("index"), payload.get("remote_index_attempt"))
        for section in sections:
            if not isinstance(section, dict):
                continue
            candidate = str(section.get("primary_character", "") or "").strip()
            if candidate:
                detected = candidate
                break
    except (OSError, ValueError, TypeError):
        pass

    explicit = str(requested or "").strip()
    ordered: list[str] = []
    # A value different from the saved project value is an intentional manual
    # override. Otherwise prefer the role detected from the remote workbook so
    # legacy projects automatically repair an ASCII/localized-name mismatch.
    if explicit and explicit.casefold() != saved.casefold():
        ordered.append(explicit)
    ordered.extend((detected, saved, explicit))

    result: list[str] = []
    seen: set[str] = set()
    for value in ordered:
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


_RELINK_ROLES = {
    "primary": ("wav_source", "wav_source_fingerprint", "english"),
    "target": ("chs_source", "chs_source_fingerprint", "chinese"),
    "reference": (
        "reference_source",
        "reference_source_fingerprint",
        "reference",
    ),
}


def _legacy_quick_source_fingerprint(
    config: ProjectConfig,
    plan_key: str,
) -> str:
    path = Path(config.root).expanduser().resolve() / ".generated" / "quick_scan.json"
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        section = payload.get(plan_key)
        if not isinstance(section, dict):
            return ""
        fingerprint = section.get("fingerprint")
        if not isinstance(fingerprint, dict):
            return ""
        return str(fingerprint.get("digest", "") or "").strip()
    except (OSError, ValueError, TypeError):
        return ""


def relink_project_source(
    config: ProjectConfig,
    role: str,
    replacement: Path,
) -> tuple[ProjectConfig, dict[str, Any]]:
    """Relink a moved voice package only after content identity verification."""
    role = str(role or "").strip().lower()
    if role not in _RELINK_ROLES:
        raise ValueError("Relink role must be primary, target, or reference")
    field_name, fingerprint_field, legacy_plan_key = _RELINK_ROLES[role]
    current_value = str(getattr(config, field_name) or "").strip()
    if not current_value:
        raise ValueError(f"Project source role {role!r} is not configured")

    expected = str(getattr(config, fingerprint_field) or "").strip()
    if not expected:
        expected = _legacy_quick_source_fingerprint(config, legacy_plan_key)
    if not expected:
        raise ValueError(
            "This project does not have a stored source fingerprint, so an automatic "
            "relink cannot prove that the replacement is the same package. "
            "Use Advanced settings if you intentionally want to replace the source."
        )

    replacement = replacement.expanduser().resolve()
    inventory = source_inventory(replacement)
    actual = str(inventory.get("fingerprint", {}).get("digest", "") or "").strip()
    if not actual or actual != expected:
        raise ValueError(
            "Replacement package fingerprint does not match the original source; "
            "automatic relink was refused."
        )

    embedded_before = bool(config.reference_text_embedded)
    update_project(
        config,
        **{
            field_name: str(replacement),
            fingerprint_field: actual,
        },
    )
    if embedded_before and role in {"primary", "reference"}:
        update_project(config, reference_text_embedded=True)

    return config, {
        "role": role,
        "previous_path": current_value,
        "replacement_path": str(replacement),
        "verified": True,
        "fingerprint": actual,
    }
