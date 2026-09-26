from __future__ import annotations

import csv
import hashlib
import http.client
import json
import logging
import os
import re
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

AI_HOBBYIST_INDEX_URLS = {
    "en": "https://raw.githubusercontent.com/AI-Hobbyist/StarRail_Voice_Sorting_Scripts/main/Indexs/EN.xlsx",
    "zh-CN": "https://raw.githubusercontent.com/AI-Hobbyist/StarRail_Voice_Sorting_Scripts/main/Indexs/CHS.xlsx",
    "ja": "https://raw.githubusercontent.com/AI-Hobbyist/StarRail_Voice_Sorting_Scripts/main/Indexs/JP.xlsx",
    "ko": "https://raw.githubusercontent.com/AI-Hobbyist/StarRail_Voice_Sorting_Scripts/main/Indexs/KR.xlsx",
}
DEFAULT_EN_INDEX_URL = AI_HOBBYIST_INDEX_URLS["en"]


def ai_hobbyist_index_url(language: str) -> str:
    normalized = str(language or "en").strip()
    aliases = {
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "chs": "zh-CN",
        "jp": "ja",
        "kr": "ko",
    }
    key = aliases.get(normalized.casefold(), normalized)
    if key not in AI_HOBBYIST_INDEX_URLS:
        raise ValueError(
            "No built-in AI-Hobbyist text index is configured for language "
            f"{language!r}; supported source languages are en, zh-CN, ja, ko"
        )
    return AI_HOBBYIST_INDEX_URLS[key]


def ai_hobbyist_index_label(url: str) -> str:
    name = Path(urlparse(url).path).name or "index.xlsx"
    return f"AI-Hobbyist {name}"
MAX_REMOTE_INDEX_BYTES = 128 * 1024**2
MAX_XLSX_UNCOMPRESSED_BYTES = 512 * 1024**2
MAX_XLSX_MEMBERS = 10_000
REMOTE_INDEX_CACHE_TTL_SECONDS = 24 * 60 * 60
REMOTE_INDEX_CACHE_DIR = Path.home() / ".hsr-voice-archive-builder" / "remote-index-cache"
# Fully-offline escape hatch: point this environment variable at a manually
# downloaded AI-Hobbyist EN/CHS/JP/KR .xlsx to skip every network download.
REMOTE_INDEX_LOCAL_FILE_ENV = "HSR_VOICE_INDEX_FILE"


def _cell(value: object) -> str:
    return "" if value is None else str(value).strip()


def _validate_xlsx_container(path: Path) -> None:
    if path.stat().st_size > MAX_REMOTE_INDEX_BYTES:
        raise ValueError(f"Remote XLSX exceeds download limit: {path.stat().st_size} bytes")

    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MAX_XLSX_MEMBERS:
                raise ValueError(f"XLSX has too many ZIP members: {len(infos)}")
            total = sum(max(0, info.file_size) for info in infos)
            if total > MAX_XLSX_UNCOMPRESSED_BYTES:
                raise ValueError(
                    "XLSX uncompressed content exceeds safety limit: "
                    f"{total} > {MAX_XLSX_UNCOMPRESSED_BYTES} bytes"
                )
            names = {info.filename for info in infos}
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise ValueError("Downloaded file is not a normal XLSX workbook")
    except zipfile.BadZipFile as exc:
        raise ValueError("Downloaded remote index is not a valid XLSX/ZIP file") from exc


def read_ai_hobbyist_xlsx(path: Path, character: str) -> list[dict[str, str]]:
    _validate_xlsx_container(path)
    try:
        # openpyxl will use defusedxml when available; it is a project
        # dependency because the remote XLSX URL is configurable.
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("Remote XLSX update checks require openpyxl") from exc

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        try:
            headers = [_cell(v) for v in next(rows)]
        except StopIteration:
            return []
        pos = {name: i for i, name in enumerate(headers)}
        required = ("语音哈希", "语音文件名", "角色", "语音文本")
        missing = [x for x in required if x not in pos]
        if missing:
            raise ValueError(f"Remote index is missing columns: {missing}")

        needle = character.strip().casefold()
        if not needle:
            raise ValueError("Remote character filter is required")

        result: list[dict[str, str]] = []
        for row in rows:
            role = _cell(row[pos["角色"]]) if pos["角色"] < len(row) else ""
            if needle not in role.casefold():
                continue
            filename = _cell(row[pos["语音文件名"]])
            if not filename:
                continue
            if not filename.lower().endswith(".wav"):
                filename += ".wav"
            battle = ""
            if "是否为战斗语音" in pos and pos["是否为战斗语音"] < len(row):
                battle = _cell(row[pos["是否为战斗语音"]])
            result.append({
                "filename": filename,
                "hash": _cell(row[pos["语音哈希"]]),
                "character": role,
                "english": _cell(row[pos["语音文本"]]),
                "battle": battle,
            })
        return result
    finally:
        wb.close()


def read_ai_hobbyist_xlsx_for_filenames(
    path: Path,
    filenames: set[str],
) -> list[dict[str, str]]:
    """Read exact requested filenames while preserving their workbook row order."""
    _validate_xlsx_container(path)
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("Remote XLSX index resolution requires openpyxl") from exc

    wanted = {Path(name).name for name in filenames}
    if not wanted:
        return []
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        try:
            headers = [_cell(v) for v in next(rows)]
        except StopIteration:
            return []
        pos = {name: i for i, name in enumerate(headers)}
        required = ("语音哈希", "语音文件名", "角色", "语音文本")
        missing = [x for x in required if x not in pos]
        if missing:
            raise ValueError(f"Remote index is missing columns: {missing}")

        result: list[dict[str, str]] = []
        for row in rows:
            filename = _cell(row[pos["语音文件名"]])
            if not filename:
                continue
            if not filename.lower().endswith(".wav"):
                filename += ".wav"
            if Path(filename).name not in wanted:
                continue
            battle = ""
            if "是否为战斗语音" in pos and pos["是否为战斗语音"] < len(row):
                battle = _cell(row[pos["是否为战斗语音"]])
            result.append({
                "filename": Path(filename).name,
                "hash": _cell(row[pos["语音哈希"]]),
                "character": _cell(row[pos["角色"]]),
                "english": _cell(row[pos["语音文本"]]),
                "battle": battle,
            })
        return result
    finally:
        wb.close()


def _download_remote_xlsx(
    url: str,
    timeout: int | tuple[float, float] = (15.0, 60.0),
    path: Path | None = None,
    *,
    max_attempts: int = 5,
    retry_delay: float = 2.0,
    has_cache_fallback: bool = False,
) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Remote index URL must be an HTTPS URL")

    if path is None:
        raise ValueError("Target path must be specified for remote XLSX download")

    if isinstance(timeout, (tuple, list)):
        connect_timeout, read_timeout = float(timeout[0]), float(timeout[1])
    else:
        connect_timeout = 15.0
        read_timeout = float(timeout)

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "HSR-Voice-Archive-Builder/0.9"},
    )

    last_error: Exception | None = None
    last_reason: str = ""

    for attempt in range(1, max_attempts + 1):
        temp_file: Path | None = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=".remote_dl_", suffix=".tmp", dir=path.parent
            )
            os.close(fd)
            temp_file = Path(temp_name)

            # Use socket timeout for connect phase
            with urllib.request.urlopen(request, timeout=connect_timeout) as response:
                status = getattr(response, "status", getattr(response, "code", 200))
                if isinstance(status, int) and status != 200:
                    raise urllib.error.HTTPError(
                        url, status, f"HTTP status {status}", response.headers, None
                    )
                elif not isinstance(status, int):
                    status = 200

                final = urlparse(response.geturl())
                if final.scheme != "https" or not final.netloc:
                    raise ValueError("Remote index redirect left HTTPS")

                declared = response.headers.get("Content-Length")
                if declared:
                    try:
                        declared_size = int(declared)
                    except ValueError:
                        declared_size = -1
                    if declared_size > MAX_REMOTE_INDEX_BYTES:
                        raise ValueError(
                            f"Remote index Content-Length exceeds safety limit: {declared_size} bytes"
                        )

                # Set socket timeout for read phase
                if hasattr(response, "fp") and hasattr(response.fp, "raw") and hasattr(response.fp.raw, "_sock"):
                    sock = response.fp.raw._sock
                    if sock:
                        sock.settimeout(read_timeout)
                elif hasattr(response, "headers") and hasattr(response, "file"):
                    # Legacy socket retrieval
                    try:
                        sock = response.file._sock
                        if sock:
                            sock.settimeout(read_timeout)
                    except AttributeError:
                        pass

                downloaded = 0
                with temp_file.open("wb") as out:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > MAX_REMOTE_INDEX_BYTES:
                            raise ValueError(
                                f"Remote index download exceeds safety limit: {downloaded} bytes"
                            )
                        out.write(chunk)

            if downloaded == 0:
                raise ValueError("Downloaded file is empty (0 bytes)")

            # Validate before moving into place
            _validate_xlsx_container(temp_file)
            os.replace(temp_file, path)
            logger.info("Remote index download attempt %d/%d succeeded from %s", attempt, max_attempts, url)
            return

        except Exception as exc:
            if temp_file and temp_file.exists():
                temp_file.unlink(missing_ok=True)

            last_error = exc
            reason_parts = [str(exc)]
            if isinstance(exc, urllib.error.URLError) and exc.reason:
                reason_parts.append(str(exc.reason))
            last_reason = " - ".join(p for p in reason_parts if p)

            # Determine if error is retryable
            is_retryable = False
            if isinstance(exc, (ssl.SSLError, socket.timeout, TimeoutError, ConnectionError, http.client.IncompleteRead)):
                is_retryable = True
            elif isinstance(exc, urllib.error.HTTPError):
                code = getattr(exc, "code", None)
                if isinstance(code, int) and (code >= 500 or code == 429):
                    is_retryable = True
            elif isinstance(exc, urllib.error.URLError):
                is_retryable = True

            if is_retryable and attempt < max_attempts:
                logger.warning(
                    "Remote index download attempt %d/%d failed (%s): %s. Retrying in %.1f seconds...",
                    attempt,
                    max_attempts,
                    url,
                    last_reason,
                    retry_delay,
                )
                time.sleep(retry_delay)
            else:
                break

    fallback_str = "Using local cache" if has_cache_fallback else "No cache available"
    detailed_msg = (
        f"Remote index download failed\n"
        f"URL: {url}\n"
        f"Attempt: {max_attempts}/{max_attempts}\n"
        f"Reason: {last_reason or str(last_error)}\n"
        f"Fallback: {fallback_str}"
    )
    raise RuntimeError(detailed_msg) from last_error


def fetch_ai_hobbyist_index(
    character: str,
    url: str = DEFAULT_EN_INDEX_URL,
    timeout: int | tuple[float, float] = (15.0, 60.0),
) -> list[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="hsr_remote_index_") as td:
        path = Path(td) / "index.xlsx"
        _download_remote_xlsx(url, timeout, path)
        return read_ai_hobbyist_xlsx(path, character)


def fetch_ai_hobbyist_index_for_filenames(
    filenames: set[str],
    url: str = DEFAULT_EN_INDEX_URL,
    timeout: int | tuple[float, float] = (15.0, 60.0),
) -> list[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="hsr_remote_index_") as td:
        path = Path(td) / "index.xlsx"
        _download_remote_xlsx(url, timeout, path)
        return read_ai_hobbyist_xlsx_for_filenames(path, filenames)


def _workbook_cache_paths(url: str, cache_dir: Path) -> tuple[Path, Path]:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return (
        cache_dir / f"workbook-{digest}.xlsx",
        cache_dir / f"workbook-{digest}.json",
    )


def _read_workbook_cache_meta(path: Path, url: str) -> dict[str, Any] | None:
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


def _write_workbook_cache_meta(path: Path, url: str, fetched_at_epoch: float) -> None:
    _atomic_write_cache(path, {
        "schema_version": 1,
        "url": url,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(fetched_at_epoch)),
        "fetched_at_epoch": fetched_at_epoch,
    })


def get_ai_hobbyist_workbook(
    url: str = DEFAULT_EN_INDEX_URL,
    *,
    timeout: int | tuple[float, float] = (15.0, 60.0),
    cache_dir: Path | None = None,
    max_age_seconds: float = REMOTE_INDEX_CACHE_TTL_SECONDS,
    max_stale_age_seconds: float = 7 * 24 * 60 * 60,
) -> tuple[Path, dict[str, Any]]:
    """Return a local copy of the remote index workbook.

    The workbook is shared by every character: one successful download serves
    all voice packages until the cache expires (fresh for 24 hours, then up
    to seven more days from the stale copy when a refresh fails), so scanning
    a different character never needs a fresh network round-trip of its own.
    """
    override = os.environ.get(REMOTE_INDEX_LOCAL_FILE_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise FileNotFoundError(
                f"{REMOTE_INDEX_LOCAL_FILE_ENV} is set but does not point "
                f"to a readable file: {path}"
            )
        _validate_xlsx_container(path)
        return path, {
            "cache_hit": True,
            "stale": False,
            "local_file": True,
            "age_seconds": 0.0,
            "fetched_at": "",
        }

    cache_root = (cache_dir or REMOTE_INDEX_CACHE_DIR).expanduser()
    xlsx_path, meta_path = _workbook_cache_paths(url, cache_root)
    meta = _read_workbook_cache_meta(meta_path, url) if meta_path.is_file() else None
    now = time.time()

    def cached_result(stale: bool) -> tuple[Path, dict[str, Any]]:
        age = max(0.0, now - float((meta or {}).get("fetched_at_epoch", 0.0)))
        return xlsx_path, {
            "cache_hit": True,
            "stale": stale,
            "age_seconds": round(age, 3),
            "fetched_at": str((meta or {}).get("fetched_at", "")),
        }

    if meta is not None and xlsx_path.is_file():
        age = max(0.0, now - float(meta.get("fetched_at_epoch", 0.0)))
        if age <= max_age_seconds:
            return cached_result(stale=False)

    cache_root.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".workbook-", suffix=".xlsx", dir=cache_root)
    os.close(fd)
    temp = Path(temp_name)
    has_valid_cache = bool(meta is not None and xlsx_path.is_file())
    try:
        _download_remote_xlsx(
            url,
            timeout,
            temp,
            has_cache_fallback=has_valid_cache,
        )
        _validate_xlsx_container(temp)
        os.replace(temp, xlsx_path)
    except Exception:
        temp.unlink(missing_ok=True)
        if meta is not None and xlsx_path.is_file():
            age = max(0.0, now - float(meta.get("fetched_at_epoch", 0.0)))
            if age <= max_stale_age_seconds:
                logger.warning(
                    "Remote index refresh failed for %s. Falling back to cached index (%s)",
                    url,
                    xlsx_path,
                )
                return cached_result(stale=True)
        raise
    _write_workbook_cache_meta(meta_path, url, now)
    return xlsx_path, {
        "cache_hit": False,
        "stale": False,
        "age_seconds": 0.0,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }


def fetch_ai_hobbyist_index_for_filenames_cached(
    filenames: set[str],
    url: str = DEFAULT_EN_INDEX_URL,
    *,
    timeout: int | tuple[float, float] = (15.0, 60.0),
    cache_dir: Path | None = None,
    max_age_seconds: float = REMOTE_INDEX_CACHE_TTL_SECONDS,
    max_stale_age_seconds: float = 7 * 24 * 60 * 60,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Resolve exact filenames against the shared, URL-cached remote workbook."""
    wanted = sorted({Path(name).name for name in filenames})
    if not wanted:
        raise ValueError("At least one WAV filename is required")
    workbook, workbook_cache = get_ai_hobbyist_workbook(
        url,
        timeout=timeout,
        cache_dir=cache_dir,
        max_age_seconds=max_age_seconds,
        max_stale_age_seconds=max_stale_age_seconds,
    )
    records = read_ai_hobbyist_xlsx_for_filenames(workbook, set(wanted))
    cache: dict[str, Any] = {
        "cache_hit": bool(workbook_cache.get("cache_hit")),
        "stale": bool(workbook_cache.get("stale")),
        "age_seconds": float(workbook_cache.get("age_seconds", 0.0)),
        "fetched_at": str(workbook_cache.get("fetched_at", "")),
    }
    if workbook_cache.get("local_file"):
        cache["local_file"] = True
    return records, cache


def _cache_path(identity: str, url: str, cache_dir: Path) -> Path:
    raw = f"{url}\0{identity}".encode("utf-8")
    return cache_dir / f"{hashlib.sha256(raw).hexdigest()}.json"


def _read_cached_records(path: Path, identity: str, url: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if (
        payload.get("schema_version") != 1
        or payload.get("url") != url
        or payload.get("identity") != identity
        or not isinstance(payload.get("records"), list)
    ):
        return None
    for row in payload["records"]:
        if not isinstance(row, dict) or not isinstance(row.get("filename"), str):
            return None
    return payload


def _atomic_write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _fetch_records_cached(
    identity: str,
    url: str,
    fetcher: Callable[[], list[dict[str, str]]],
    *,
    cache_dir: Path | None = None,
    max_age_seconds: int = REMOTE_INDEX_CACHE_TTL_SECONDS,
    max_stale_age_seconds: int = 7 * 24 * 60 * 60,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    cache_root = (cache_dir or REMOTE_INDEX_CACHE_DIR).expanduser()
    path = _cache_path(identity, url, cache_root)
    cached = _read_cached_records(path, identity, url) if path.is_file() else None
    now = time.time()
    if cached is not None:
        age = max(0.0, now - float(cached.get("fetched_at_epoch", 0.0)))
        if age <= max_age_seconds:
            return list(cached["records"]), {
                "cache_hit": True,
                "stale": False,
                "age_seconds": round(age, 3),
                "fetched_at": cached.get("fetched_at", ""),
            }

    try:
        records = fetcher()
    except Exception:
        if cached is None:
            raise
        age = max(0.0, now - float(cached.get("fetched_at_epoch", 0.0)))
        if age > max_stale_age_seconds:
            raise
        return list(cached["records"]), {
            "cache_hit": True,
            "stale": True,
            "age_seconds": round(age, 3),
            "fetched_at": cached.get("fetched_at", ""),
        }

    fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    _atomic_write_cache(path, {
        "schema_version": 1,
        "url": url,
        "identity": identity,
        "fetched_at": fetched_at,
        "fetched_at_epoch": now,
        "records": records,
    })
    return records, {
        "cache_hit": False,
        "stale": False,
        "age_seconds": 0.0,
        "fetched_at": fetched_at,
    }


def fetch_ai_hobbyist_index_cached(
    character: str,
    url: str = DEFAULT_EN_INDEX_URL,
    *,
    timeout: int | tuple[float, float] = (15.0, 60.0),
    cache_dir: Path | None = None,
    max_age_seconds: int = REMOTE_INDEX_CACHE_TTL_SECONDS,
    max_stale_age_seconds: int = 7 * 24 * 60 * 60,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    character_key = character.strip().casefold()
    if not character_key:
        raise ValueError("Remote character filter is required")
    return _fetch_records_cached(
        f"character:{character_key}",
        url,
        lambda: fetch_ai_hobbyist_index(character, url, timeout=timeout),
        cache_dir=cache_dir,
        max_age_seconds=max_age_seconds,
        max_stale_age_seconds=max_stale_age_seconds,
    )


def remote_update_plan(
    manifest_path: Path,
    records: list[dict[str, str]],
    *,
    url: str = DEFAULT_EN_INDEX_URL,
    queried_character: str = "",
) -> dict[str, Any]:
    from .diff import classify_names

    result = classify_names(manifest_path, [row["filename"] for row in records])
    details = {Path(row["filename"]).name: row for row in records}
    by_filename: dict[str, list[dict[str, str]]] = {}
    for row in records:
        by_filename.setdefault(Path(row["filename"]).name, []).append(row)

    # One basename with conflicting upstream records cannot be resolved to a
    # single existing/new audio member just by taking the last workbook row.
    conflicts = {
        name for name, candidates in by_filename.items()
        if len({(str(row.get("hash") or "").lower(), str(row.get("english") or ""))
                for row in candidates}) > 1
    }
    if conflicts:
        for key in ("exact_existing", "new_logical"):
            result[key] = [name for name in result[key] if Path(str(name)).name not in conflicts]
        result["variant_of_existing"] = [
            item for item in result["variant_of_existing"]
            if Path(str(item["candidate"])).name not in conflicts
        ]
        result["ambiguous"].extend({
            "candidate": name, "reason": "conflicting_remote_rows",
            "records": by_filename[name],
        } for name in sorted(conflicts))

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    manifest_entries = manifest.get("entries", []) if isinstance(manifest, dict) else manifest
    existing_by_name: dict[str, list[dict[str, Any]]] = {}
    for row in manifest_entries:
        if isinstance(row, dict):
            existing_by_name.setdefault(Path(str(row.get("filename") or "")).name, []).append(row)

    changed: list[dict[str, Any]] = []
    unchanged: list[str] = []
    for name in result["exact_existing"]:
        current = existing_by_name.get(Path(str(name)).name, [])
        new = details.get(Path(str(name)).name, {})
        if len(current) != 1 or not new:
            unchanged.append(name)
            continue
        old = current[0]
        changes = []
        old_text = str(old.get("source_text") or old.get("english") or "").strip()
        new_text = str(new.get("english") or "").strip()
        if new_text and old_text != new_text:
            changes.append("source_text")
        old_hash = str(old.get("sha256") or "").strip().lower()
        new_hash = str(new.get("hash") or "").strip().lower()
        if (re.fullmatch(r"[0-9a-f]{64}", old_hash)
                and re.fullmatch(r"[0-9a-f]{64}", new_hash)
                and old_hash != new_hash):
            changes.append("audio_sha256")
        if changes:
            changed.append({
                "filename": name,
                "changes": changes,
                "previous": {"source_text": old_text, "sha256": old_hash},
                "metadata": new,
            })
        else:
            unchanged.append(name)
    result["exact_existing"] = unchanged
    result["changed_existing"] = changed
    for key in ("exact_existing", "new_logical"):
        result[key] = [
            {"filename": name, "metadata": details.get(Path(str(name)).name, {})}
            if isinstance(name, str) else name
            for name in result[key]
        ]
    for item in result["variant_of_existing"]:
        item["metadata"] = details.get(Path(str(item["candidate"])).name, {})

    result["counts"] = {
        key: len(result[key])
        for key in ("exact_existing", "variant_of_existing", "new_logical", "changed_existing", "ambiguous")
    }

    character_counts = Counter(
        str(row.get("character", "") or "").strip()
        for row in records
        if str(row.get("character", "") or "").strip()
    )
    primary_character = (
        character_counts.most_common(1)[0][0] if character_counts else ""
    )
    result["provider"] = ai_hobbyist_index_label(url)
    result["character"] = primary_character
    result["queried_character"] = queried_character
    result["character_counts"] = dict(character_counts.most_common())
    result["remote_rows"] = len(records)
    return result


def exclude_applied_updates(plan: dict[str, Any], apply_report_path: Path) -> dict[str, Any]:
    """Move successfully applied filenames out of a repeated remote-update plan."""
    try:
        report = json.loads(apply_report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return plan
    applied = {
        Path(str(name)).name
        for name in report.get("applied_filenames", [])
        if str(name).strip()
    }
    if not applied:
        return plan
    pending = []
    moved = []
    for item in plan.get("new_logical", []):
        name = item.get("filename", "") if isinstance(item, dict) else str(item)
        if Path(name).name in applied:
            moved.append(item)
        else:
            pending.append(item)
    if moved:
        plan["new_logical"] = pending
        plan.setdefault("exact_existing", []).extend(moved)
        counts = plan.setdefault("counts", {})
        counts["new_logical"] = len(pending)
        counts["exact_existing"] = len(plan["exact_existing"])
        plan["already_applied_count"] = len(moved)
        plan["applied"] = not pending
    return plan


def exclude_indexed_updates(plan: dict[str, Any], index_path: Path | None) -> dict[str, Any]:
    """Treat files already adopted into the current project index as applied."""
    if index_path is None or not index_path.is_file():
        return plan
    try:
        with index_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return plan
    indexed = {
        Path(str(row.get("filename", ""))).name
        for row in rows
        if str(row.get("filename", "")).strip()
    }
    if not indexed:
        return plan
    pending = []
    moved = []
    for item in plan.get("new_logical", []):
        name = item.get("filename", "") if isinstance(item, dict) else str(item)
        (moved if Path(name).name in indexed else pending).append(item)
    if moved:
        plan["new_logical"] = pending
        plan.setdefault("exact_existing", []).extend(moved)
        counts = plan.setdefault("counts", {})
        counts["new_logical"] = len(pending)
        counts["exact_existing"] = len(plan["exact_existing"])
        plan["already_applied_count"] = int(plan.get("already_applied_count", 0)) + len(moved)
        plan["applied"] = not pending
    return plan
