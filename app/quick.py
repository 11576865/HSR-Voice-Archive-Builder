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

from .builder import (
    MAX_ARCHIVE_MEMBERS,
    atomic_write_text,
    cross_language_voice_key,
    ensure_dir_or_extract,
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

    wavs = [Path(item["name"]).name for item in files if str(item["name"]).lower().endswith(".wav")]
    labs = [Path(item["name"]).stem for item in files if str(item["name"]).lower().endswith(".lab")]
    wav_stems = {Path(name).stem for name in wavs}
    lab_stems = set(labs)
    duplicate_wavs = sorted(name for name, count in Counter(wavs).items() if count > 1)

    # Track the top-level folder of every WAV so mixed-language packages can be
    # split into a primary audio folder and reference-text folder(s) instead of
    # failing on duplicate basenames (see analyze_language_folders).
    wav_folder_names: dict[str, list[str]] = {}
    for item in files:
        name = str(item["name"])
        if not name.lower().endswith(".wav"):
            continue
        parts = PurePosixPath(name).parts
        folder = parts[0] if len(parts) > 1 else ""
        wav_folder_names.setdefault(folder, []).append(Path(name).name)
    same_folder_duplicates: set[str] = set()
    for names in wav_folder_names.values():
        for name, count in Counter(names).items():
            if count > 1:
                same_folder_duplicates.add(name)

    return {
        "source": str(source),
        "kind": "directory" if source.is_dir() else source.suffix.lower().lstrip("."),
        "file_count": len(files),
        "wav_count": len(wavs),
        "lab_count": len(labs),
        "wav_names": wavs,
        "lab_names": labs,
        "wav_folder_names": wav_folder_names,
        "wav_folders": {
            folder: len(names) for folder, names in sorted(wav_folder_names.items())
        },
        "duplicate_wav_names": duplicate_wavs,
        "same_folder_duplicate_wavs": sorted(same_folder_duplicates),
        "cross_folder_duplicate_wavs": sorted(set(duplicate_wavs) - same_folder_duplicates),
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


_LANGUAGE_FOLDER_KEYWORDS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("zh-CN", re.compile(r"(^|[^a-z])(chs|cn)([^a-z]|$)|chinese|mandarin|中文|中国|国语|简体", re.IGNORECASE)),
    ("en", re.compile(r"(^|[^a-z])(en|english|eng|英)([^a-z]|$)", re.IGNORECASE)),
    ("ja", re.compile(r"(^|[^a-z])(jp|ja|japanese|日)([^a-z]|$)", re.IGNORECASE)),
    ("ko", re.compile(r"(^|[^a-z])(kr|ko|korean|韩)([^a-z]|$)", re.IGNORECASE)),
)
_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")
_MAX_FOLDER_LAB_SAMPLES = 8


def _detect_lab_language(texts: list[str]) -> str:
    """Classify sampled LAB text as zh-CN/en; '' when there is no evidence.

    Japanese/Korean LABs also contain CJK characters, so content detection
    deliberately reports zh-CN for any CJK-dominant sample; folder-name
    keywords above can still distinguish ja/ko before content is consulted.
    """
    cjk = 0
    latin = 0
    for text in texts:
        cjk += len(_CJK_RE.findall(text))
        latin += len(re.findall(r"[A-Za-z]", text))
    if cjk > 0:
        return "zh-CN"
    if latin > 0:
        return "en"
    return ""


def _folder_lab_paths(source: Path, folder: str) -> list[str]:
    """Relative LAB member paths inside one top-level folder of the source."""
    if source.is_dir():
        root = source / folder if folder else source
        if not root.is_dir():
            return []
        return [
            path.relative_to(source).as_posix()
            for path in sorted(root.rglob("*.lab"))
        ]
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as z:
            return sorted(
                name for name in z.namelist()
                if name.lower().endswith(".lab")
                and _top_folder_of(name) == folder
            )
    if source.is_file() and source.suffix.lower() == ".7z":
        exe = shutil.which("7zz") or shutil.which("7z")
        if not exe:
            return []
        listed = subprocess.run(
            [exe, "l", "-slt", str(source)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            check=False,
        )
        if listed.returncode != 0:
            return []
        return sorted(
            record.get("Path", "")
            for record in _parse_7z_slt(listed.stdout)
            if record.get("Path", "").lower().endswith(".lab")
            and not record.get("Attributes", "").startswith("D")
            and _top_folder_of(record.get("Path", "")) == folder
        )
    return []


def _top_folder_of(name: str) -> str:
    parts = PurePosixPath(str(name).replace("\\", "/")).parts
    return parts[0] if len(parts) > 1 else ""


def _folder_members(extracted: Path, folder: str, pattern: str) -> list[Path]:
    """Files matching `pattern` inside one materialized top-level folder.

    Root-level files (folder == "") are listed non-recursively so pruning or
    merging a root reference folder cannot touch sibling language folders.
    """
    root = extracted / folder if folder else extracted
    if not folder:
        return sorted(root.glob(pattern))
    return sorted(root.rglob(pattern))


def _folder_lab_stems(source: Path, folder: str) -> set[str]:
    return {Path(name).stem for name in _folder_lab_paths(source, folder)}


def _folder_lab_sample(source: Path, folder: str) -> list[str]:
    """Read up to a few LAB texts from one folder to detect its language."""
    texts: list[str] = []
    paths = _folder_lab_paths(source, folder)[:_MAX_FOLDER_LAB_SAMPLES]
    if source.is_dir():
        for rel in paths:
            try:
                texts.append((source / rel).read_text(encoding="utf-8-sig", errors="replace").strip())
            except OSError:
                continue
    elif source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as z:
            for rel in paths:
                try:
                    texts.append(z.read(rel).decode("utf-8-sig", errors="replace").strip())
                except (KeyError, UnicodeDecodeError, zipfile.BadZipFile):
                    continue
    # .7z content sampling is impractical through the 7-Zip CLI; folder-name
    # keywords remain the only signal there (handled by the caller).
    return [text for text in texts if text]


def analyze_language_folders(
    source: Path,
    wav_folders: dict[str, int],
    source_text_language: str,
) -> dict[str, Any]:
    """Split a mixed-language package into primary audio / reference text roles.

    Cross-folder duplicate basenames usually mean one folder per dub language.
    The folder whose language matches source_text_language supplies the audio;
    the other folders contribute same-stem LAB text only (their audio never
    enters the continuous FLAC, mirroring the official-Chinese-package flow).
    """
    folders = [folder for folder, count in wav_folders.items() if int(count) > 0]
    if not folders:
        raise ValueError("no WAV folders were found")

    detected: dict[str, str] = {}
    for folder in folders:
        language = ""
        for language_code, pattern in _LANGUAGE_FOLDER_KEYWORDS:
            if folder and pattern.search(folder):
                language = language_code
                break
        if not language:
            language = _detect_lab_language(_folder_lab_sample(source, folder))
        if language:
            detected[folder] = language
    undetected = [folder for folder in folders if folder not in detected]
    if undetected:
        raise ValueError(
            "这些文件夹的语言无法识别（请在文件夹名中加入 en/CHS/中文 等关键词）: "
            + ", ".join(folder or "<根目录>" for folder in undetected)
        )

    primary_candidates = [
        folder for folder, language in detected.items() if language == source_text_language
    ]
    if not primary_candidates:
        raise ValueError(
            f"没有与源文本语言 {source_text_language} 对应的主音频文件夹；"
            f"检测到: "
            + ", ".join(f"{folder or '<根目录>'}={language}" for folder, language in sorted(detected.items()))
        )
    if len(primary_candidates) > 1:
        raise ValueError(
            "多个文件夹同时匹配源文本语言 "
            f"{source_text_language}，无法确定主音频文件夹: "
            + ", ".join(folder or "<根目录>" for folder in primary_candidates)
        )
    primary = primary_candidates[0]
    references = [folder for folder in folders if folder != primary]
    if not references:
        raise ValueError("只有主音频文件夹，没有可参考的其他语言文件夹")
    return {
        "primary": primary,
        "reference": references,
        "detected": detected,
        "primary_wav_count": int(wav_folders.get(primary, 0)),
        "reference_lab_count": sum(
            len(_folder_lab_stems(source, folder)) for folder in references
        ),
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

    remote_index_url = remote_index_url.strip()
    if not remote_index_url:
        try:
            remote_index_url = ai_hobbyist_index_url(source_text_language)
        except ValueError:
            # AI-Hobbyist currently publishes EN/CHS/JP/KR indexes. Other
            # source languages can still use EN only for ordering when the
            # primary audio package supplies complete same-stem LAB text.
            complete_primary_lab = (
                int(english.get("wav_count", 0)) > 0
                and int(english.get("wav_lab_pairs", 0))
                == int(english.get("wav_count", 0))
            )
            if source_text_language != "en" and complete_primary_lab:
                remote_index_url = DEFAULT_EN_INDEX_URL
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
    language_folders: dict[str, Any] | None = None
    if english.get("same_folder_duplicate_wavs"):
        blockers.append(
            "Duplicate WAV basenames were found in the primary source: "
            + ", ".join(english["same_folder_duplicate_wavs"][:5])
        )
    elif english.get("cross_folder_duplicate_wavs"):
        try:
            language_folders = analyze_language_folders(
                Path(english["source"]),
                {str(k): int(v) for k, v in (english.get("wav_folders") or {}).items()},
                source_text_language,
            )
        except ValueError as exc:
            blockers.append(
                "Duplicate WAV basenames were found in the primary source: "
                + ", ".join(english["cross_folder_duplicate_wavs"][:5])
                + f"; automatic language-folder split failed: {exc}"
            )
        else:
            references = ", ".join(f"'{f}'" for f in language_folders["reference"])
            warnings.append(
                "Mixed-language package detected: folder "
                f"'{language_folders['primary']}' ({language_folders['primary_wav_count']} voices) "
                f"is the primary audio; same-stem LABs from {references} "
                f"({language_folders['reference_lab_count']} entries) provide official "
                "Chinese subtitles; reference audio is excluded from the continuous FLAC"
            )
    if language_folders:
        wav_names = set(english["wav_folder_names"][language_folders["primary"]])
    else:
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
    total_wavs = len(wav_names)

    def _index_coverage_key(index: dict[str, Any]) -> tuple[int, int]:
        return (int(index.get("matched_wavs", 0)), int(index.get("english_matched", 0)))

    local_full = bool(
        selected_index
        and int(selected_index["matched_wavs"]) == total_wavs
        and int(selected_index["english_matched"]) == total_wavs
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
    if selected_index is None:
        blockers.append("No reliable local or remote index covers the package WAV names")
    else:
        matched_wavs = int(selected_index["matched_wavs"])
        text_matched = int(selected_index["english_matched"])
        coverage = matched_wavs / max(1, total_wavs)
        if matched_wavs == 0:
            selected_index = None
            blockers.append("No reliable local or remote index covers the package WAV names")
        elif coverage < MIN_INDEX_COVERAGE:
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
    if index_usable and selected_index is not None:
        if selected_index.get("source") == "remote":
            index_rows, _ambiguous_rows = _remote_rows(
                remote_records,
                wav_names,
                str(selected_index.get("provider") or ai_hobbyist_index_label(remote_index_url)),
            )
        else:
            index_rows = [
                row for row in normalize_index(Path(selected_index["path"]))
                if Path(str(row.get("filename", ""))).name in wav_names
            ]
        chinese_stems = set(chs.get("lab_names", [])) if chs else set()
        if language_folders and chs is None:
            for folder in language_folders["reference"]:
                chinese_stems.update(_folder_lab_stems(Path(english["source"]), folder))
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
    translation_estimate = estimate_workload_tokens(pending_records, 80)

    return {
        "schema_version": 2,
        "kind": "quick_scan",
        "source_text_language": source_text_language,
        "english": {
            key: value for key, value in english.items()
            if key not in {"wav_names", "lab_names", "wav_folder_names"}
        },
        "chinese": (
            {
                key: value
                for key, value in chs.items()
                if key not in {"wav_names", "lab_names", "wav_folder_names"}
            }
            if chs
            else None
        ),
        "language_folders": language_folders,
        "reference_index_attempt": reference_index_attempt,
        "reference_text_match": reference_text_match,
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
    language_folders = plan.get("language_folders") or None
    if language_folders:
        wanted = set(current_inventory["wav_folder_names"][language_folders["primary"]])
    else:
        wanted = set(current_inventory["wav_names"])
    ambiguous: list[str] = []
    if selected_index.get("source") == "remote":
        remote_records, _ = fetch_ai_hobbyist_index_for_filenames_cached(
            wanted,
            str(selected_index["url"]),
        )
        if _index_fingerprint(remote_records) != selected_index["records_fingerprint"]:
            raise RuntimeError("Remote index changed after Quick Scan; scan again before building")
        filtered, ambiguous = _remote_rows(
            remote_records,
            wanted,
            str(selected_index.get("provider") or ai_hobbyist_index_label(str(selected_index["url"]))),
        )
    else:
        local_index = Path(selected_index["path"])
        if sha256_file(local_index) != selected_index["file_sha256"]:
            raise RuntimeError("Local index changed after Quick Scan; scan again before building")
        original_rows = normalize_index(local_index)
        filtered = [row for row in original_rows if Path(row["filename"]).name in wanted]
    # Files the index cannot order reliably keep their audio but join the
    # archive as an unindexed appendix at the end, without subtitles.
    uncovered = sorted(
        (wanted - {Path(str(row.get("filename", ""))).name for row in filtered})
        | set(ambiguous)
    )
    for name in uncovered:
        filtered.append({
            "index": str(len(filtered) + 1),
            "group": parse_voice_identity(name).group,
            "filename": name,
            "source": "unindexed-package-file",
            "source_detail": "",
            "english": "",
            "reference_text": "",
            "reference_language": "",
            "official_target_text": "",
            "official_target_language": "",
            "official_target_source": "",
            "sha256": "",
        })
    if len(filtered) != len(wanted):
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
        "sha256",
    ]
    with generated_index.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(filtered)

    # Mixed-language packages are materialized once so builds never see the
    # original mixed archive: the primary folder becomes the audio source and
    # reference folders contribute same-stem LAB text only (their audio never
    # enters the continuous FLAC, mirroring the official-Chinese-package flow).
    primary_wav_source = english_source
    official_chs_source = chs_source
    wav_fingerprint_value = str(
        current_inventory.get("fingerprint", {}).get("digest", "") or ""
    )
    chs_fingerprint_value = str(
        (current_chs or {}).get("fingerprint", {}).get("digest", "") or ""
    )
    if language_folders:
        extracted = ensure_dir_or_extract(
            english_source, generated_dir, "language_folders"
        )
        primary_dir = extracted / language_folders["primary"]
        if not primary_dir.is_dir():
            raise RuntimeError(
                f"Primary audio folder '{language_folders['primary']}' is missing "
                "after extraction"
            )
        reference_dirs: list[Path] = []
        for folder in language_folders["reference"]:
            ref_dir = extracted / folder if folder else extracted
            if not ref_dir.is_dir():
                raise RuntimeError(
                    f"Reference LAB folder '{folder or '<root>'}' is missing after extraction"
                )
            reference_dirs.append(ref_dir)
            if english_source.is_file():
                # Prune reference audio after extraction; only LAB text is kept.
                for wav in sorted(_folder_members(extracted, folder, "*.wav")):
                    wav.unlink()
        if official_chs_source is None and reference_dirs:
            if len(reference_dirs) == 1:
                official_chs_source = reference_dirs[0]
            else:
                merged_labs = generated_dir / "official_chs_labs"
                if merged_labs.exists():
                    shutil.rmtree(merged_labs)
                merged_labs.mkdir(parents=True)
                for folder in language_folders["reference"]:
                    for lab in sorted(_folder_members(extracted, folder, "*.lab")):
                        target = merged_labs / lab.name
                        if target.exists() and target.read_bytes() != lab.read_bytes():
                            raise RuntimeError(
                                f"Reference folders disagree on LAB text for {lab.name}"
                            )
                        shutil.copy2(lab, target)
                official_chs_source = merged_labs
        primary_wav_source = primary_dir
        wav_fingerprint_value = str(
            source_inventory(primary_wav_source).get("fingerprint", {}).get("digest", "")
            or ""
        )
        if official_chs_source is not None:
            chs_fingerprint_value = str(
                source_inventory(official_chs_source).get("fingerprint", {}).get("digest", "")
                or ""
            )

    config = create_project(
        project_root,
        name=name.strip() or _safe_project_name(plan, english_source),
        index_csv=str(generated_index),
        wav_source=str(primary_wav_source),
        output_dir="output",
        chs_source=str(official_chs_source) if official_chs_source else "",
        reference_source=str(reference_source) if reference_source else "",
        wav_source_fingerprint=wav_fingerprint_value,
        chs_source_fingerprint=chs_fingerprint_value,
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
