from __future__ import annotations

import tempfile
import urllib.request
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

DEFAULT_EN_INDEX_URL = "https://raw.githubusercontent.com/AI-Hobbyist/StarRail_Voice_Sorting_Scripts/main/Indexs/EN.xlsx"


def _cell(value: object) -> str:
    return "" if value is None else str(value).strip()


def read_ai_hobbyist_xlsx(path: Path, character: str) -> list[dict[str, str]]:
    try:
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


def fetch_ai_hobbyist_index(
    character: str,
    url: str = DEFAULT_EN_INDEX_URL,
    timeout: int = 90,
) -> list[dict[str, str]]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Remote index URL must be an HTTPS URL")
    request = urllib.request.Request(url, headers={"User-Agent": "HSR-Voice-Archive-Builder/0.3"})
    with tempfile.TemporaryDirectory(prefix="hsr_remote_index_") as td:
        path = Path(td) / "EN.xlsx"
        with urllib.request.urlopen(request, timeout=timeout) as response, path.open("wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        return read_ai_hobbyist_xlsx(path, character)


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
    result["provider"] = "AI-Hobbyist EN.xlsx"
    result["character"] = records[0]["character"] if records else ""
    result["remote_rows"] = len(records)
    return result
