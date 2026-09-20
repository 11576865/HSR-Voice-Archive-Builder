from __future__ import annotations

import hashlib
import html
import json
import mmap
import random
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


DATASET = "simon3000/starrail-voice"
ROWS_URL = "https://datasets-server.huggingface.co/rows"
RESULT_URL = (
    "https://huggingface.co/datasets/simon3000/starrail-voice/"
    "resolve/main/result.json?download=true"
)
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


def download_result_json(destination: Path, progress: Callable[[str], None] | None = None) -> Path:
    """Resume the dataset metadata download without replacing a valid local copy."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".part")
    if destination.is_file() and destination.stat().st_size > 10 * 1024 * 1024:
        return destination
    for attempt in range(8):
        start = part.stat().st_size if part.exists() else 0
        request = urllib.request.Request(RESULT_URL, headers={"User-Agent": "HSR-Voice-Archive-Builder/0.9"})
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


def resolve_targets(result_json: Path, targets: list[dict[str, str]]) -> dict[str, Any]:
    """Resolve AI-Hobbyist rows to exact Dataset Viewer row indices conservatively."""
    by_name = {Path(str(t.get("filename", ""))).stem: t for t in targets}
    by_hash = {str(t.get("hash", "")).casefold(): t for t in targets if t.get("hash")}
    by_media = {}
    for target in targets:
        stem = Path(str(target.get("filename", ""))).stem
        match = re.search(r"_(\d+)$", stem) if stem.casefold().startswith("ev_archive_") else None
        if match:
            by_media[match.group(1)] = target
    by_path = {f"english/voice/{name}.wem".casefold(): t for name, t in by_name.items()}
    resolved: dict[str, dict[str, Any]] = {}
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
            if key.casefold().startswith("english/"):
                stem = Path(key).stem
                ingame = _field(chunk, "inGameFilename")
                transcription = _field(chunk, "transcription")
                speaker = _field(chunk, "speaker")
                voice_id = _field(chunk, "voiceID")
                row = {"row_idx": row_index, "key": key, "speaker": speaker, "transcription": transcription}
                target = by_path.get(ingame.casefold()) if ingame else None
                method = "inGameFilename"
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
    unresolved = [str(t.get("filename", "")) for t in targets if str(t.get("filename", "")) not in resolved]
    return {"dataset": DATASET, "total_rows": total, "targets": resolved, "unresolved": unresolved}


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


def download_resolved_audio(plan: dict[str, Any], destination: Path, progress: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    targets = plan.get("targets", {})
    blocks: dict[int, list[tuple[str, int]]] = {}
    for filename, row in targets.items():
        index = int(row["row_idx"])
        blocks.setdefault(index // 100 * 100, []).append((filename, index))
    completed, failed = [], []
    total = len(targets)
    for start, group in sorted(blocks.items()):
        payload = _json_get(ROWS_URL, {"dataset": DATASET, "config": "default", "split": "train", "offset": start, "length": 100})
        rows = {int(item["row_idx"]): item for item in payload.get("rows", []) if item.get("row_idx") is not None}
        for filename, index in group:
            stem = Path(filename).stem
            existing = list(destination.glob(stem + ".*"))
            if existing:
                completed.append({"filename": filename, "status": "exists", "path": str(existing[0])})
                continue
            item = rows.get(index)
            src = _audio_src((item or {}).get("row", {}).get("audio"))
            if not src:
                failed.append({"filename": filename, "reason": "audio.src 不可用"})
                continue
            try:
                request = urllib.request.Request(src, headers={"User-Agent": "HSR-Voice-Archive-Builder/0.9"})
                with urllib.request.urlopen(request, timeout=120) as response:
                    data = response.read()
                    content_type = response.headers.get("Content-Type", "").casefold()
                if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
                    extension = ".wav"
                elif data[:4] == b"fLaC":
                    extension = ".flac"
                elif data[:4] == b"OggS":
                    extension = ".ogg"
                else:
                    extension = ".wav" if "wav" in content_type else ".bin"
                output = destination / (stem + extension)
                temporary = output.with_suffix(output.suffix + ".part")
                temporary.write_bytes(data)
                temporary.replace(output)
                completed.append({"filename": filename, "status": "ok", "path": str(output), "sha256": hashlib.sha256(data).hexdigest()})
            except Exception as exc:
                failed.append({"filename": filename, "reason": f"{type(exc).__name__}: {exc}"})
            if progress:
                progress(len(completed) + len(failed), total, filename)
    return {"completed": completed, "failed": failed, "unresolved": plan.get("unresolved", [])}
