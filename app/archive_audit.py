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


def verify_archive_source_audio(
    config: Any,
    output_dir: Path,
    *,
    report_progress: Any = None,
) -> dict[str, Any]:
    """Stream real source WAV hashes and compare against the archive manifest.

    The original source directory or existing reference extraction path is
    resolved once, and only one WAV is streamed at a time. This never edits
    the manifest, generated FLAC, subtitle overrides, or alignment sidecar.
    ZIP/7z source packages may be extracted into the normal .state cache.
    """
    from .builder import collect_wav_members, resolve_member_wav, sha256_file
    from .project import resolve_project_path
    from .reference_workbench import _preview_root

    document = json.loads((Path(output_dir) / "manifest.json").read_text(encoding="utf-8"))
    entries = document.get("entries") if isinstance(document, dict) else None
    if not isinstance(entries, list) or any(not isinstance(x, dict) for x in entries):
        raise ValueError("Manifest 必须包含 entries 对象数组")

    if report_progress:
        report_progress("resolve", "正在定位原始语音包并检查来源成员身份", 0, max(1, len(entries)))
    source = resolve_project_path(config, config.wav_source)
    if source is None or not source.exists():
        raise FileNotFoundError("项目原始 WAV/ZIP/7z 音频源不存在，无法核对字节")
    source = source.resolve()
    source_before = source.stat() if source.is_file() else None
    root = _preview_root(config).resolve()
    members = collect_wav_members(root)
    counts = {
        "verified": 0, "mismatch": 0, "missing": 0,
        "unverifiable": 0, "ambiguous": 0, "changed_during_verify": 0,
    }
    group_counts: dict[str, dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []
    verified_bytes = 0

    for index, entry in enumerate(entries, start=1):
        entry_id = str(entry.get("index", entry.get("id", index)))
        group = str(entry.get("group") or "").strip() or "未分组"
        per_group = group_counts.setdefault(group, {
            "name": group, "verified": 0, "mismatch": 0,
            "missing": 0, "unverifiable": 0, "ambiguous": 0,
            "changed_during_verify": 0,
        })
        code = ""
        detail = ""
        if report_progress:
            report_progress(
                "hash-source",
                f"正在核验原始 WAV {index}/{len(entries)} · #{entry_id}",
                index - 1, max(1, len(entries)),
            )
        expected = str(entry.get("sha256") or "").strip().lower()
        member = str(entry.get("source_member_id") or "").replace("\\", "/").strip()
        filename = str(entry.get("filename") or "").strip()
        if not _SHA256.fullmatch(expected):
            code, detail = "unverifiable", "Manifest 未提供合法的原始 WAV SHA-256"
        else:
            try:
                audio, resolved_member = resolve_member_wav(members, filename, member)
                if audio is None:
                    code, detail = "missing", f"原始语音源不存在：{resolved_member or filename}"
                elif (not audio.resolve().is_relative_to(root)
                      or audio.is_symlink()):
                    code, detail = "unverifiable", "来源成员路径离开项目源根目录或是符号链接"
                else:
                    before = audio.stat()
                    actual = sha256_file(audio).lower()
                    after = audio.stat()
                    if (before.st_size != after.st_size
                            or before.st_mtime_ns != after.st_mtime_ns):
                        code, detail = "changed_during_verify", "WAV 在流式校验期间发生变化"
                    elif actual != expected:
                        code, detail = "mismatch", (
                            f"{resolved_member} · expected {expected[:16]}…"
                            f" / observed {actual[:16]}…"
                        )
                    else:
                        code = "verified"
                        verified_bytes += after.st_size
            except ValueError as exc:
                code, detail = "ambiguous", str(exc)
            except (OSError, RuntimeError) as exc:
                code, detail = "unverifiable", f"{type(exc).__name__}: {exc}"
        counts[code] += 1
        per_group[code] += 1
        if code != "verified" and len(findings) < MAX_ISSUES:
            findings.append({
                "id": entry_id, "group": group, "source_member_id": member,
                "code": code, "detail": detail,
            })

    if source_before is not None:
        source_after = source.stat()
        if (source_before.st_size != source_after.st_size
                or source_before.st_mtime_ns != source_after.st_mtime_ns):
            # Do not certify a mutable input package on a single hash pass.
            findings.insert(0, {
                "id": "", "group": "", "source_member_id": "",
                "code": "source_package_changed",
                "detail": "原始压缩包在核验期间发生变化；本轮结果不能作为完整有效性证明",
            })
            return {
                "scope": "source-wav-byte-audit",
                "source_package_changed": True,
                "summary": counts | {"total": len(entries), "verified_bytes": verified_bytes,
                                      "trusted_complete": False},
                "groups": list(group_counts.values()),
                "findings": findings[:MAX_ISSUES],
            }

    if report_progress:
        report_progress("complete", "原始 WAV 字节核验结束", len(entries), max(1, len(entries)))
    return {
        "scope": "source-wav-byte-audit",
        "source_package_changed": False,
        "summary": counts | {
            "total": len(entries), "verified_bytes": verified_bytes,
            "trusted_complete": len(entries) > 0 and counts["verified"] == len(entries),
        },
        "groups": list(group_counts.values()),
        "findings": findings,
    }
