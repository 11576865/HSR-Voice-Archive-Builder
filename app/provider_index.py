from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .game_profiles import game_index_kind, game_index_url, game_provider, normalize_language
from .remote_index import (
    REMOTE_INDEX_CACHE_TTL_SECONDS,
    fetch_ai_hobbyist_index,
    fetch_ai_hobbyist_index_for_filenames_cached,
)

MAX_PROVIDER_JSON_BYTES = 128 * 1024**2
PROVIDER_INDEX_CACHE_DIR = Path.home() / ".hsr-voice-archive-builder" / "provider-index-cache"
GENSHIN_INDEX_LOCAL_FILE_ENV = "GENSHIN_VOICE_INDEX_FILE"


def provider_index_label(game_id: str, url: str) -> str:
    provider = game_provider(game_id)
    name = Path(urlparse(str(url or "")).path).name or "index"
    if str(provider.get("game_id", "")) == "honkai-star-rail":
        return f"AI-Hobbyist {name}"
    return f"AI-Hobbyist {provider.get('label', game_id)} {name}"


def provider_order_basis(game_id: str) -> str:
    return "json_source_order" if game_index_kind(game_id) == "json" else "workbook_row_order"


def _safe_https_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Remote provider index URL must be an HTTPS URL")


def _cache_paths(url: str, cache_dir: Path) -> tuple[Path, Path]:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_dir / f"index-{digest}.json", cache_dir / f"index-{digest}.meta.json"


def _read_meta(path: Path, url: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if (
        payload.get("schema_version") != 1
        or payload.get("url") != url
        or not isinstance(payload.get("fetched_at_epoch"), (int, float))
    ):
        return None
    return payload


def _write_meta(path: Path, url: str, fetched_at: float) -> None:
    payload = {
        "schema_version": 1,
        "url": url,
        "fetched_at_epoch": fetched_at,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(fetched_at)),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _validate_json_index(path: Path) -> None:
    size = path.stat().st_size
    if size <= 2:
        raise ValueError("Provider JSON index is empty")
    if size > MAX_PROVIDER_JSON_BYTES:
        raise ValueError(
            f"Provider JSON index exceeds safety limit: {size} > {MAX_PROVIDER_JSON_BYTES} bytes"
        )
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Provider index is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Provider JSON index root must be an object")


def _download_json_index(url: str, destination: Path, timeout: float = 120.0) -> None:
    _safe_https_url(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".provider-index-", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temporary = Path(temp_name)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "HSR-Voice-Archive-Builder/0.9"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final = urlparse(response.geturl())
            if final.scheme != "https" or not final.netloc:
                raise ValueError("Provider index redirect left HTTPS")
            declared = response.headers.get("Content-Length")
            if declared:
                try:
                    declared_size = int(declared)
                except ValueError:
                    declared_size = -1
                if declared_size > MAX_PROVIDER_JSON_BYTES:
                    raise ValueError(
                        f"Provider JSON Content-Length exceeds safety limit: {declared_size} bytes"
                    )
            total = 0
            with temporary.open("wb") as output:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_PROVIDER_JSON_BYTES:
                        raise ValueError("Provider JSON download exceeds safety limit")
                    output.write(block)
        _validate_json_index(temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def get_provider_json_index(
    url: str,
    *,
    cache_dir: Path | None = None,
    max_age_seconds: float = REMOTE_INDEX_CACHE_TTL_SECONDS,
    max_stale_age_seconds: float = 7 * 24 * 60 * 60,
) -> tuple[Path, dict[str, Any]]:
    override = os.environ.get(GENSHIN_INDEX_LOCAL_FILE_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise FileNotFoundError(
                f"{GENSHIN_INDEX_LOCAL_FILE_ENV} is set but does not point to a readable file: {path}"
            )
        _validate_json_index(path)
        return path, {
            "cache_hit": True,
            "stale": False,
            "local_file": True,
            "age_seconds": 0.0,
            "fetched_at": "",
        }

    root = (cache_dir or PROVIDER_INDEX_CACHE_DIR).expanduser()
    data_path, meta_path = _cache_paths(url, root)
    meta = _read_meta(meta_path, url) if meta_path.is_file() else None
    now = time.time()

    def cached(stale: bool) -> tuple[Path, dict[str, Any]]:
        age = max(0.0, now - float((meta or {}).get("fetched_at_epoch", 0.0)))
        return data_path, {
            "cache_hit": True,
            "stale": stale,
            "age_seconds": round(age, 3),
            "fetched_at": str((meta or {}).get("fetched_at", "")),
        }

    if meta is not None and data_path.is_file():
        age = max(0.0, now - float(meta["fetched_at_epoch"]))
        if age <= max_age_seconds:
            _validate_json_index(data_path)
            return cached(False)

    root.mkdir(parents=True, exist_ok=True)
    try:
        _download_json_index(url, data_path)
        fetched = time.time()
        _write_meta(meta_path, url, fetched)
        return data_path, {
            "cache_hit": False,
            "stale": False,
            "age_seconds": 0.0,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(fetched)),
        }
    except Exception:
        if meta is not None and data_path.is_file():
            age = max(0.0, now - float(meta["fetched_at_epoch"]))
            if age <= max_stale_age_seconds:
                _validate_json_index(data_path)
                return cached(True)
        raise


def _basename_from_source(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    name = Path(normalized).name
    if not name:
        return ""
    if name.casefold().endswith(".wem"):
        name = name[:-4] + ".wav"
    elif not Path(name).suffix:
        name += ".wav"
    return name


def _genshin_record(key: str, row: Any) -> dict[str, str] | None:
    if not isinstance(row, dict):
        return None
    source_name = (
        row.get("sourceFileName")
        or row.get("fileName")
        or row.get("inGameFilename")
        or ""
    )
    filename = _basename_from_source(str(source_name))
    if not filename:
        return None
    character = str(
        row.get("talkName")
        or row.get("npcName")
        or row.get("speaker")
        or row.get("avatarName")
        or ""
    ).strip()
    text = str(
        row.get("voiceContent")
        or row.get("text")
        or row.get("transcription")
        or ""
    ).strip()
    return {
        "filename": filename,
        "hash": str(key or "").strip(),
        "character": character,
        "english": text,
        "battle": "",
        "source_path": str(source_name),
        "avatar_name": str(row.get("avatarName") or "").strip(),
    }


def read_genshin_json_for_filenames(path: Path, filenames: set[str]) -> list[dict[str, str]]:
    wanted = {Path(name).name.casefold(): Path(name).name for name in filenames}
    if not wanted:
        return []
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Genshin provider index root must be an object")
    result: list[dict[str, str]] = []
    for key, row in payload.items():
        record = _genshin_record(str(key), row)
        if record is None:
            continue
        wanted_name = wanted.get(record["filename"].casefold())
        if wanted_name is None:
            continue
        record["filename"] = wanted_name
        result.append(record)
    return result


def _compact_token(value: str) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def read_genshin_json_for_character(path: Path, character: str) -> list[dict[str, str]]:
    needle = _compact_token(character)
    if not needle:
        raise ValueError("Remote character filter is required")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Genshin provider index root must be an object")
    result: list[dict[str, str]] = []
    for key, row in payload.items():
        record = _genshin_record(str(key), row)
        if record is None:
            continue
        haystacks = (
            record.get("filename", ""),
            record.get("character", ""),
            record.get("source_path", ""),
            record.get("avatar_name", ""),
        )
        if any(needle in _compact_token(value) for value in haystacks):
            result.append(record)
    return result


def fetch_provider_index_for_filenames_cached(
    game_id: str,
    filenames: set[str],
    *,
    language: str = "en",
    url: str = "",
    cache_dir: Path | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    resolved_url = str(url or game_index_url(game_id, language))
    kind = game_index_kind(game_id)
    if kind == "xlsx":
        return fetch_ai_hobbyist_index_for_filenames_cached(
            filenames,
            resolved_url,
            cache_dir=cache_dir,
        )
    if kind == "json":
        path, meta = get_provider_json_index(resolved_url, cache_dir=cache_dir)
        return read_genshin_json_for_filenames(path, filenames), meta
    raise ValueError(f"No remote index provider is configured for {game_id!r}")


def fetch_provider_index(
    game_id: str,
    character: str,
    *,
    language: str = "en",
    url: str = "",
    cache_dir: Path | None = None,
) -> list[dict[str, str]]:
    resolved_url = str(url or game_index_url(game_id, language))
    kind = game_index_kind(game_id)
    if kind == "xlsx":
        return fetch_ai_hobbyist_index(character, resolved_url)
    if kind == "json":
        path, _ = get_provider_json_index(resolved_url, cache_dir=cache_dir)
        return read_genshin_json_for_character(path, character)
    raise ValueError(f"No remote index provider is configured for {game_id!r}")
