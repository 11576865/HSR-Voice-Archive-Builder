from __future__ import annotations

import hashlib
import html
import json
import mmap
import os
import tempfile
import ipaddress
import random
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .game_profiles import normalize_language


DATASET = "simon3000/starrail-voice"
ROWS_URL = "https://datasets-server.huggingface.co/rows"
MAX_AUDIO_DOWNLOAD_BYTES = 128 * 1024 * 1024


def result_url(dataset: str = DATASET) -> str:
    dataset = str(dataset or DATASET).strip()
    return f"https://huggingface.co/datasets/{dataset}/resolve/main/result.json?download=true"
ENTRY_RE = re.compile(br'"([^"\\]+\.wav)":\{"filename":')
JSON_STR = rb'"(?:\\.|[^"\\])*"'


def _field(chunk: bytes, name: str) -> str:
    match = re.search(rb'"' + name.encode("ascii") + rb'":(' + JSON_STR + rb')', chunk)
    if not match:
        return ""
    try:
        return str(json.loads(match.group(1).decode("utf-8")) or "")
    except (UnicodeDecodeError, ValueError, TypeError):
        return ""


def _normal_text(value: str) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", "", value)
    value = unicodedata.normalize("NFKC", value)
    value = value.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"}))
    return re.sub(r"\s+", " ", value).strip()


def _voice_relative_path(value: str) -> str:
    """Return a language-independent in-game voice path."""
    normalized = str(value or "").replace("\\", "/").strip().casefold().lstrip("/")
    if not normalized:
        return ""
    marker = "/voice/"
    if marker in "/" + normalized:
        return ("/" + normalized).split(marker, 1)[1]
    prefixes = (
        "english(us)/", "english/", "chinese(prc)/", "chinese/",
        "japanese/", "korean/", "en-us/", "zh-cn/", "ja-jp/", "ko-kr/",
    )
    for prefix in prefixes:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return normalized


def download_result_json(
    destination: Path,
    progress: Callable[[str], None] | None = None,
    *,
    dataset: str = DATASET,
) -> Path:
    """Resume the dataset metadata download without replacing a valid local copy."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".part")
    if destination.is_file() and destination.stat().st_size > 10 * 1024 * 1024:
        return destination
    for attempt in range(8):
        start = part.stat().st_size if part.exists() else 0
        request = urllib.request.Request(result_url(dataset), headers={"User-Agent": "HSR-Voice-Archive-Builder/0.9"})
        if start:
            request.add_header("Range", f"bytes={start}-")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                mode = "ab" if start and response.status == 206 else "wb"
                if progress:
                    progress(f"正在下载 Hugging Face 定位索引：已续传 {start / 1024**2:.1f} MiB")
                with part.open(mode) as output:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        output.write(block)
            if part.stat().st_size <= 10 * 1024 * 1024:
                raise RuntimeError("下载的 result.json 异常过小")
            part.replace(destination)
            return destination
        except (OSError, urllib.error.URLError, RuntimeError):
            if attempt == 7:
                raise
            time.sleep(min(30, 2 ** (attempt + 1)) + random.random())
    raise RuntimeError("无法下载 result.json")


def resolve_targets(
    result_json: Path,
    targets: list[dict[str, str]],
    *,
    dataset: str = DATASET,
    source_language: str = "en",
) -> dict[str, Any]:
    """Resolve only the selected primary audio locale; pair Chinese as optional reference.

    Matching by filename, media ID or text must never silently select an
    English dataset row for a Japanese/Korean/Chinese source package.
    """
    language = normalize_language(source_language)
    folders = {
        "en": {"english", "english(us)"},
        "zh-CN": {"chinese(prc)", "chinese"},
        "ja": {"japanese", "japanese(jp)"},
        "ko": {"korean", "korean(kr)"},
    }
    if language not in folders:
        raise ValueError(f"Unsupported incremental audio language: {source_language!r}")
    primary_folders = folders[language]
    by_name = {Path(str(t.get("filename", ""))).stem: t for t in targets}
    by_hash = {str(t.get("hash", "")).casefold(): t for t in targets if t.get("hash")}
    by_media = {}
    for target in targets:
        stem = Path(str(target.get("filename", ""))).stem
        match = re.search(r"_(\d+)$", stem) if stem.casefold().startswith("ev_archive_") else None
        if match:
            by_media[match.group(1)] = target
    by_path = {
        f"{folder}/voice/{name}.wem".casefold(): t
        for folder in primary_folders for name, t in by_name.items()
    }
    by_ingame_basename = {(Path(name).stem + ".wem").casefold(): t for name, t in by_name.items()}
    resolved: dict[str, dict[str, Any]] = {}
    chinese_by_path: dict[str, dict[str, Any]] = {}
    text_rows: dict[str, list[dict[str, Any]]] = {}
    total = 0
    with result_json.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        iterator = ENTRY_RE.finditer(mm)
        current = next(iterator, None)
        while current is not None:
            following = next(iterator, None)
            end = following.start() if following is not None else len(mm)
            chunk = mm[current.start():end]
            key = current.group(1).decode("utf-8", "replace")
            row_index = total
            total += 1
            language_prefix = key.split("/", 1)[0].casefold()
            if language_prefix in primary_folders or language_prefix == "chinese(prc)":
                stem = Path(key).stem
                ingame = _field(chunk, "inGameFilename")
                transcription = _field(chunk, "transcription")
                speaker = _field(chunk, "speaker")
                voice_id = _field(chunk, "voiceID")
                row = {
                    "row_idx": row_index,
                    "key": key,
                    "speaker": speaker,
                    "transcription": transcription,
                    "ingame_filename": ingame,
                }
                relative = _voice_relative_path(ingame)
                if language_prefix == "chinese(prc)" and language != "zh-CN":
                    if relative:
                        chinese_by_path.setdefault(relative, row)
                    current = following
                    continue
                row["audio_language"] = language
                target = by_path.get(ingame.casefold()) if ingame else None
                method = "inGameFilename"
                if target is None and ingame:
                    ingame_base = Path(ingame.replace("\\", "/")).name.casefold()
                    target = by_ingame_basename.get(ingame_base)
                    if target is not None:
                        method = "inGameFilenameBasename"
                if target is None:
                    target, method = by_hash.get(stem.casefold()), "wav_hash"
                if target is None:
                    target, method = by_media.get(stem) or by_media.get(voice_id), "media_id"
                if target is not None:
                    resolved.setdefault(str(target["filename"]), {**row, "method": method})
                if transcription:
                    text_rows.setdefault(_normal_text(transcription), []).append(row)
            current = following
    for target in targets:
        filename = str(target.get("filename", ""))
        if filename in resolved:
            continue
        candidates = text_rows.get(_normal_text(str(target.get("english", ""))), [])
        if len(candidates) == 1:
            resolved[filename] = {**candidates[0], "method": "unique_transcription"}
    reference_targets: dict[str, dict[str, Any]] = {}
    for filename, english_row in resolved.items():
        relative = _voice_relative_path(str(english_row.get("ingame_filename", "")))
        if not relative:
            relative = Path(filename).with_suffix(".wem").name.casefold()
        chinese = chinese_by_path.get(relative)
        if chinese is not None:
            reference_targets[filename] = {
                **chinese,
                "method": "same_ingame_filename",
                "reference_language": "zh-CN",
                "audio_language": "zh-CN",
            }
    unresolved = [str(t.get("filename", "")) for t in targets if str(t.get("filename", "")) not in resolved]
    return {
        "dataset": dataset,
        "audio_language": language,
        "total_rows": total,
        "targets": resolved,
        "reference_targets": reference_targets,
        "unresolved": unresolved,
    }


def _json_get(url: str, params: dict[str, Any], attempts: int = 7) -> dict[str, Any]:
    target = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(target, headers={"User-Agent": "HSR-Voice-Archive-Builder/0.9"})
            with urllib.request.urlopen(request, timeout=75) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and not 500 <= exc.code < 600:
                raise
            wait = int(exc.headers.get("Retry-After", "0") or 0) or min(65, 2 ** (attempt + 1))
        except (OSError, urllib.error.URLError):
            wait = min(45, 2 ** (attempt + 1))
        if attempt == attempts - 1:
            raise RuntimeError(f"Hugging Face 请求失败：{target}")
        time.sleep(wait + random.random())
    raise RuntimeError("Hugging Face 请求失败")

_AUDIO_LOCALE_LABELS = {
    "english": "en", "english(us)": "en", "en": "en",
    "chinese": "zh-CN", "chinese(prc)": "zh-CN", "zh-cn": "zh-CN",
    "japanese": "ja", "japanese(jp)": "ja", "ja": "ja",
    "korean": "ko", "korean(kr)": "ko", "ko": "ko",
}


def _dataset_locale(raw: object) -> str:
    value = str(raw or "").strip().casefold()
    return _AUDIO_LOCALE_LABELS.get(value, normalize_language(value))


def _verify_dataset_audio_row(row: Any, target: dict[str, Any], language: str) -> None:
    """An expected row_idx is not proof that /rows still returns the same locale."""
    if not isinstance(row, dict):
        raise ValueError("Hugging Face dataset row is missing")
    expected = normalize_language(language)
    declared_raw = str(row.get("language") or "").strip()
    actual_filename = str(
        row.get("ingame_filename") or row.get("inGameFilename") or ""
    ).strip()
    declared_locale = _dataset_locale(declared_raw) if declared_raw else ""
    path_folder = actual_filename.replace("\\", "/").split("/", 1)[0]
    path_locale = _dataset_locale(path_folder) if "/" in actual_filename.replace("\\", "/") else ""
    if declared_locale and declared_locale != expected:
        raise ValueError(
            f"Hugging Face audio locale mismatch: expected {expected}, got {declared_raw}"
        )
    if path_locale and path_locale != expected:
        raise ValueError(
            f"Hugging Face in-game voice path locale mismatch: expected {expected}, got {path_folder}"
        )
    if not declared_locale and not path_locale:
        raise ValueError("Hugging Face dataset row has no verifiable audio locale")
    original = str(target.get("ingame_filename") or "").strip()
    if original and actual_filename and _voice_relative_path(original) != _voice_relative_path(actual_filename):
        raise ValueError("Hugging Face dataset row identity changed since result.json resolution")


def _audio_src(value: Any) -> str:
    if isinstance(value, dict):
        src = value.get("src")
        if isinstance(src, str) and src.startswith(("https://", "http://")):
            return src
        for nested in value.values():
            found = _audio_src(nested)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _audio_src(nested)
            if found:
                return found
    return ""


def _audio_kind(header: bytes) -> str:
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return ".wav"
    if header.startswith(b"fLaC"):
        return ".flac"
    if header.startswith(b"OggS"):
        return ".ogg"
    return ""


def _existing_audio(destination: Path, stem: str, expected_ext: str) -> Path | None:
    """A stale .part, other extension, or broken header is never a download."""
    if expected_ext not in {".wav", ".flac", ".ogg"}:
        return None
    for ext in (expected_ext,):
        path = destination / (stem + ext)
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if path.stat().st_size >= 16:
                with path.open("rb") as stream:
                    if _audio_kind(stream.read(16)) == ext:
                        return path
        except OSError:
            continue
    return None


def _require_public_audio_url(url: str) -> None:
    """Do not let dataset-provided links contact loopback or private IPs."""
    split = urllib.parse.urlsplit(url)
    host = split.hostname or ""
    if split.scheme != "https" or not host or split.username or split.password:
        raise ValueError("Only public HTTPS audio URLs are permitted")
    if host.lower() == "localhost" or host.lower().endswith((".localhost", ".local", ".internal")):
        raise ValueError("Local audio endpoints are not permitted")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return  # A DNS name still needs network egress restrictions at deployment.
    if not addr.is_global:
        raise ValueError("Private and non-global audio IP endpoints are not permitted")


class _GuardedAudioRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_public_audio_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_resolved_audio(
    plan: dict[str, Any],
    destination: Path,
    progress: Callable[[int, int, str], None] | None = None,
    *,
    dataset: str = "",
) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    dataset = str(dataset or plan.get("dataset") or DATASET)
    targets = plan.get("targets", {})
    plan_language = str(plan.get("audio_language") or "").strip()
    if plan_language:
        normalized = normalize_language(plan_language)
        for name, row in targets.items():
            if normalize_language(str(row.get("audio_language") or "")) != normalized:
                raise ValueError(
                    f"Audio locale mismatch for {name}: expected {normalized}, "
                    f"got {row.get('audio_language')!r}"
                )
    blocks: dict[int, list[tuple[str, int]]] = {}
    for filename, row in targets.items():
        index = int(row["row_idx"])
        blocks.setdefault(index // 100 * 100, []).append((filename, index))
    completed, failed = [], []
    total = len(targets)
    opener = urllib.request.build_opener(_GuardedAudioRedirect())
    for start, group in sorted(blocks.items()):
        payload = _json_get(ROWS_URL, {"dataset": dataset, "config": "default", "split": "train", "offset": start, "length": 100})
        rows = {int(item["row_idx"]): item for item in payload.get("rows", []) if item.get("row_idx") is not None}
        for filename, index in group:
            stem = Path(filename).stem
            expected_ext = Path(filename).suffix.lower()
            existing = _existing_audio(destination, stem, expected_ext)
            if existing is not None:
                completed.append({"filename": filename, "status": "exists", "path": str(existing)})
                if progress:
                    progress(len(completed) + len(failed), total, filename)
                continue
            item = rows.get(index)
            remote_row = (item or {}).get("row", {})
            if plan_language:
                try:
                    _verify_dataset_audio_row(remote_row, targets[filename], normalized)
                except ValueError as exc:
                    failed.append({"filename": filename, "reason": str(exc)})
                    if progress:
                        progress(len(completed) + len(failed), total, filename)
                    continue
            src = _audio_src(remote_row.get("audio") if isinstance(remote_row, dict) else None)
            if not src:
                failed.append({"filename": filename, "reason": "audio.src 不可用"})
                if progress:
                    progress(len(completed) + len(failed), total, filename)
                continue
            temporary: Path | None = None
            try:
                _require_public_audio_url(src)
                request = urllib.request.Request(src, headers={"User-Agent": "HSR-Voice-Archive-Builder/0.9"})
                fd, temp_name = tempfile.mkstemp(prefix=".hsr-audio-", suffix=".part", dir=destination)
                temporary = Path(temp_name)
                digest = hashlib.sha256()
                header = b""
                count = 0
                with os.fdopen(fd, "wb") as stream, opener.open(request, timeout=120) as response:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        count += len(block)
                        if count > MAX_AUDIO_DOWNLOAD_BYTES:
                            raise ValueError("Audio download exceeds the 128 MiB budget")
                        if len(header) < 16:
                            header = (header + block)[:16]
                        digest.update(block)
                        stream.write(block)
                    stream.flush()
                    os.fsync(stream.fileno())
                extension = _audio_kind(header)
                if not extension or count < 16:
                    raise ValueError("Downloaded content is not WAV, FLAC, or Ogg audio")
                if extension != expected_ext:
                    raise ValueError("Downloaded audio format does not match the indexed member suffix")
                output = destination / (stem + extension)
                if output.is_symlink():
                    raise ValueError("Unsafe existing audio symlink")
                os.replace(temporary, output)
                temporary = None
                completed.append({"filename": filename, "status": "ok", "path": str(output), "sha256": digest.hexdigest()})
            except Exception as exc:
                failed.append({"filename": filename, "reason": f"{type(exc).__name__}: {exc}"})
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            if progress:
                progress(len(completed) + len(failed), total, filename)
    return {"completed": completed, "failed": failed, "unresolved": plan.get("unresolved", [])}


def confirmed_reference_metadata(
    plan: dict[str, Any],
    download: dict[str, Any],
) -> dict[str, dict[str, str]]:
    """Return Chinese reference text only for rows whose audio was obtained."""
    completed = {
        Path(str(row.get("filename", ""))).name
        for row in download.get("completed", [])
        if isinstance(row, dict)
    }
    result: dict[str, dict[str, str]] = {}
    for name, row in plan.get("targets", {}).items():
        filename = Path(str(name)).name
        transcription = str(row.get("transcription", "") or "").strip()
        if filename in completed and transcription:
            result[filename] = {
                "reference_text": transcription,
                "reference_language": str(row.get("reference_language", "zh-CN") or "zh-CN"),
            }
    return result
