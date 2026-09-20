from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

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


def _download_remote_xlsx(url: str, timeout: int, path: Path) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Remote index URL must be an HTTPS URL")

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "HSR-Voice-Archive-Builder/0.9"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response, path.open("wb") as out:
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

        downloaded = 0
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


def fetch_ai_hobbyist_index(
    character: str,
    url: str = DEFAULT_EN_INDEX_URL,
    timeout: int = 90,
) -> list[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="hsr_remote_index_") as td:
        path = Path(td) / "index.xlsx"
        _download_remote_xlsx(url, timeout, path)
        return read_ai_hobbyist_xlsx(path, character)


def fetch_ai_hobbyist_index_for_filenames(
    filenames: set[str],
    url: str = DEFAULT_EN_INDEX_URL,
    timeout: int = 90,
) -> list[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="hsr_remote_index_") as td:
        path = Path(td) / "index.xlsx"
        _download_remote_xlsx(url, timeout, path)
        return read_ai_hobbyist_xlsx_for_filenames(path, filenames)


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
    timeout: int = 90,
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


def fetch_ai_hobbyist_index_for_filenames_cached(
    filenames: set[str],
    url: str = DEFAULT_EN_INDEX_URL,
    *,
    timeout: int = 90,
    cache_dir: Path | None = None,
    max_age_seconds: int = REMOTE_INDEX_CACHE_TTL_SECONDS,
    max_stale_age_seconds: int = 7 * 24 * 60 * 60,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    wanted = sorted({Path(name).name for name in filenames})
    if not wanted:
        raise ValueError("At least one WAV filename is required")
    names_digest = hashlib.sha256("\n".join(wanted).encode("utf-8")).hexdigest()
    return _fetch_records_cached(
        f"filenames:{names_digest}",
        url,
        lambda: fetch_ai_hobbyist_index_for_filenames(set(wanted), url, timeout=timeout),
        cache_dir=cache_dir,
        max_age_seconds=max_age_seconds,
        max_stale_age_seconds=max_stale_age_seconds,
    )


def remote_update_plan(manifest_path: Path, records: list[dict[str, str]]) -> dict[str, Any]:
    from .diff import classify_names

    result = classify_names(manifest_path, [row["filename"] for row in records])
    details = {row["filename"]: row for row in records}
    for key in ("exact_existing", "new_logical"):
        result[key] = [
            {"filename": name, "metadata": details.get(name, {})}
            if isinstance(name, str) else name
            for name in result[key]
        ]
    for item in result["variant_of_existing"]:
        item["metadata"] = details.get(item["candidate"], {})
    result["provider"] = ai_hobbyist_index_label(DEFAULT_EN_INDEX_URL)
    result["character"] = records[0]["character"] if records else ""
    result["remote_rows"] = len(records)
    return result
