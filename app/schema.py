from __future__ import annotations

import csv
from pathlib import Path

from .identity import parse_voice_identity

INDEX_ALIASES = {
    "index": ("index", "序号", "编号"),
    "group": ("group", "分组", "major_group"),
    "filename": ("filename", "文件名", "file", "wav", "wav_filename"),
    "source": ("source", "来源"),
    "source_detail": ("source_detail", "来源细分"),
    "english": ("english", "ENGLISH", "英文", "英文文本", "lab_text_escaped", "语音文本"),
    "sha256": ("sha256", "SHA-256", "sha-256"),
    "reference_text": ("reference_text", "REFERENCE_TEXT", "参考文本"),
    "reference_language": ("reference_language", "REFERENCE_LANGUAGE", "参考语言"),
    "official_target_text": ("official_target_text", "OFFICIAL_TARGET_TEXT", "官方目标文本"),
    "official_target_language": ("official_target_language", "OFFICIAL_TARGET_LANGUAGE", "官方目标语言"),
    "official_target_source": ("official_target_source", "OFFICIAL_TARGET_SOURCE", "官方目标来源"),
    "source_member_id": ("source_member_id", "SOURCE_MEMBER_ID", "来源成员路径", "member_path"),
}
BILINGUAL_ALIASES = {
    "filename": INDEX_ALIASES["filename"],
    "english": INDEX_ALIASES["english"],
    "chinese": ("chinese", "CHINESE", "中文", "中文文本", "translation"),
    "source_member_id": INDEX_ALIASES["source_member_id"],
}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def pick(row: dict[str, str], aliases: tuple[str, ...], default: str = "") -> str:
    for key in aliases:
        if key in row and row[key] is not None:
            value = str(row[key]).strip()
            if value:
                return value
    return default


def normalize_index(path: Path) -> list[dict[str, str]]:
    rows = read_rows(path)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for pos, row in enumerate(rows, 1):
        filename = pick(row, INDEX_ALIASES["filename"])
        if not filename:
            raise ValueError(f"Index row {pos} has no filename")
        member_id = pick(row, INDEX_ALIASES["source_member_id"])
        # Rows carrying a package member path are distinct even when they share
        # a basename; rows without one keep the legacy filename uniqueness.
        key = member_id or filename
        if key in seen:
            raise ValueError(f"Duplicate filename in index: {key}")
        seen.add(key)
        ident = parse_voice_identity(filename)
        out.append({
            "index": pick(row, INDEX_ALIASES["index"], str(pos)),
            "group": pick(row, INDEX_ALIASES["group"], ident.group),
            "filename": filename,
            "source": pick(row, INDEX_ALIASES["source"]),
            "source_detail": pick(row, INDEX_ALIASES["source_detail"]),
            "english": pick(row, INDEX_ALIASES["english"]),
            "sha256": pick(row, INDEX_ALIASES["sha256"]).lower(),
            "reference_text": pick(row, INDEX_ALIASES["reference_text"]),
            "reference_language": pick(row, INDEX_ALIASES["reference_language"]),
            "official_target_text": pick(row, INDEX_ALIASES["official_target_text"]),
            "official_target_language": pick(row, INDEX_ALIASES["official_target_language"]),
            "official_target_source": pick(row, INDEX_ALIASES["official_target_source"]),
            "source_member_id": member_id,
        })
    return out


def normalize_bilingual(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    out: dict[str, dict[str, str]] = {}
    for pos, row in enumerate(read_rows(path), 1):
        filename = pick(row, BILINGUAL_ALIASES["filename"])
        if not filename:
            raise ValueError(f"Bilingual row {pos} has no filename")
        key = pick(row, BILINGUAL_ALIASES["source_member_id"]) or filename
        if key in out:
            raise ValueError(f"Duplicate filename in bilingual CSV: {key}")
        out[key] = {
            "english": pick(row, BILINGUAL_ALIASES["english"]),
            "chinese": pick(row, BILINGUAL_ALIASES["chinese"]),
        }
    return out


def write_legacy_inputs(index_path: Path, bilingual_path: Path | None, dest: Path) -> tuple[Path, Path]:
    index = normalize_index(index_path)
    bilingual = normalize_bilingual(bilingual_path)
    dest.mkdir(parents=True, exist_ok=True)
    legacy_index = dest / "index.csv"
    legacy_bilingual = dest / "bilingual.csv"

    with legacy_index.open("w", encoding="utf-8-sig", newline="") as f:
        fields = [
            "序号", "分组", "文件名", "来源", "来源细分", "来源成员路径",
            "英文文本", "参考文本", "参考语言", "官方目标文本", "官方目标语言", "官方目标来源", "SHA-256",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in index:
            member_id = str(row.get("source_member_id", "") or "")
            b = bilingual.get(member_id or row["filename"], {})
            english = row["english"] or b.get("english", "")
            # Rows without source text are allowed: Quick Mode appends files
            # the remote index cannot order as an unindexed appendix. They
            # keep their audio and render without subtitles.
            w.writerow({
                "序号": row["index"], "分组": row["group"], "文件名": row["filename"],
                "来源": row["source"], "来源细分": row["source_detail"],
                "来源成员路径": member_id, "英文文本": english,
                "参考文本": row.get("reference_text", ""),
                "参考语言": row.get("reference_language", ""),
                "官方目标文本": row.get("official_target_text", ""),
                "官方目标语言": row.get("official_target_language", ""),
                "官方目标来源": row.get("official_target_source", ""),
                "SHA-256": row["sha256"],
            })

    with legacy_bilingual.open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["文件名", "来源成员路径", "中文", "ENGLISH"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in index:
            member_id = str(row.get("source_member_id", "") or "")
            b = bilingual.get(member_id or row["filename"], {})
            w.writerow({
                "文件名": row["filename"],
                "来源成员路径": member_id,
                "中文": b.get("chinese", ""),
                "ENGLISH": row["english"] or b.get("english", ""),
            })
    return legacy_index, legacy_bilingual
