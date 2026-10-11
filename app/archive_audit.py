"""Read-only HSR archive chapter coverage and manifest provenance diagnostics.

This does not verify the bytes of the original WAV files, decode FLAC,
or alter the canonical manifest. These are structural observations only.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_ISSUES = 100


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def audit_archive(output_dir: Path) -> dict[str, Any]:
    manifest_file = Path(output_dir) / "manifest.json"
    if not manifest_file.is_file():
        raise FileNotFoundError("请先构建档案；尚不存在 manifest.json")
    payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise ValueError("Manifest 必须包含 entries 数组")
    entries = payload["entries"]
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Manifest entries 包含非对象条目")

    groups: dict[str, dict[str, Any]] = {}
    member_ids: set[str] = set()
    entry_ids: set[str] = set()
    basename_counts: Counter[str] = Counter()
    issues: list[dict[str, str]] = []
    issue_count = 0
    source_count = target_count = audio_hash_count = 0
    previous_start: float | None = None

    def issue(code: str, entry_id: str, detail: str) -> None:
        nonlocal issue_count
        issue_count += 1
        if len(issues) < MAX_ISSUES:
            issues.append({"code": code, "id": entry_id, "detail": detail})

    for index, entry in enumerate(entries):
        entry_id = str(entry.get("index", entry.get("id", index + 1)))
        group = str(entry.get("group") or "").strip() or "未分组"
        member = str(entry.get("source_member_id") or "").replace("\\", "/").strip()
        filename = str(entry.get("filename") or "").strip()
        basename_counts[filename] += 1
        group_info = groups.setdefault(group, {
            "name": group, "entries": 0,
            "source_text_present": 0, "target_text_present": 0,
        })
        group_info["entries"] += 1
        has_source = bool(str(entry.get("source_text") or entry.get("english") or "").strip())
        has_target = bool(str(entry.get("target_text") or entry.get("chinese") or "").strip())
        source_count += has_source
        target_count += has_target
        group_info["source_text_present"] += has_source
        group_info["target_text_present"] += has_target

        if entry_id in entry_ids:
            issue("duplicate_entry_id", entry_id, "重复档案条目 ID")
        entry_ids.add(entry_id)
        if member:
            if member in member_ids:
                issue("duplicate_source_member", entry_id, member)
            member_ids.add(member)
        else:
            issue("source_member_missing", entry_id, filename or "文件名也缺失")
        if not has_source:
            issue("source_text_missing", entry_id, "没有可追溯的原文台词")
        if not has_target:
            issue("target_text_missing", entry_id, "尚无目标语言文本")

        sha = str(entry.get("sha256") or "").strip()
        if _SHA256.fullmatch(sha):
            audio_hash_count += 1
        else:
            issue("source_audio_hash_missing", entry_id, "原始 WAV SHA-256 缺失或格式非法")

        start = _number(entry.get("start_seconds"))
        audio_end = _number(entry.get("audio_end_seconds"))
        display_end = _number(entry.get("display_end_seconds", audio_end))
        if (start is None or audio_end is None or display_end is None
                or start < 0 or audio_end <= start or display_end <= start):
            issue("invalid_source_timeline", entry_id, "音频或字幕显示时间戳无效")
        else:
            if previous_start is not None and start < previous_start - 0.000001:
                issue("non_monotonic_timeline", entry_id, "原始条目开始时间倒退")
            previous_start = start

    return {
        "schema_version": 1, "scope": "manifest-structural-only",
        "summary": {
            "entries": len(entries), "groups": len(groups),
            "source_text_present": source_count, "target_text_present": target_count,
            "audio_hash_present": audio_hash_count,
            "duplicate_basename_groups": sum(count > 1 for count in basename_counts.values()),
            "issues": issue_count, "issues_truncated": issue_count > len(issues),
        },
        "groups": list(groups.values()), "issues": issues,
        "checked_files": {
            name: (Path(output_dir) / name).is_file()
            for name in ("timeline_resolved.json", "continuous.flac",
                         "HSR_Voice_Archive.srt", "HSR_Voice_Archive.ass")
        },
    }
