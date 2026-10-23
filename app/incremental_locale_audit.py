"""Read-only source provenance audit for historical cross-locale HSR updates.

Never infer spoken language from a WAV header. Older incremental apply receipts
were English-only, so they are a useful warning, not audio-content validation.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .game_profiles import normalize_language
from .project import ProjectConfig, resolve_project_path


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def audit_incremental_audio_locale(config: ProjectConfig) -> dict[str, Any]:
    """Locate immutable original-source candidates without mutating a project."""
    root = Path(config.root).resolve()
    generated = root / ".generated"
    output = resolve_project_path(config, config.output_dir)
    current = resolve_project_path(config, config.wav_source)
    current_index = resolve_project_path(config, config.index_csv)
    original_index = generated / "quick_index.csv"
    quick_scan = generated / "quick_scan.json"
    language = normalize_language(str(config.audio_language or "auto"))
    issues: list[str] = []
    history: dict[str, Any] = {"exists": False, "applied": False, "audio_language": ""}
    original: Path | None = None
    origin_fingerprint = ""
    scan_valid = False

    if _regular_file(quick_scan):
        try:
            payload = json.loads(quick_scan.read_text(encoding="utf-8"))
            primary = payload.get("english", {})
            if not isinstance(primary, dict):
                raise ValueError("Invalid primary source metadata")
            raw = str(primary.get("source") or "").strip()
            if raw:
                path = Path(raw).expanduser()
                original = (root / path).resolve() if not path.is_absolute() else path.resolve()
            fp = primary.get("fingerprint", {})
            if isinstance(fp, dict):
                origin_fingerprint = str(fp.get("digest") or "")
            scan_valid = True
        except (OSError, ValueError, TypeError):
            issues.append("原始 Quick Scan 元数据不可用；不能推断初始语音包。")

    report_path = output / "update_apply_report.json" if output else None
    if report_path and _regular_file(report_path):
        history["exists"] = True
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(report, dict):
                raise ValueError("Invalid incremental report")
            history["applied"] = bool(report.get("project_updated"))
            # Prior to the locale fix, successful main downloads selected English.
            history["audio_language"] = str(report.get("audio_language") or "en")
            history["count"] = len(report.get("applied_filenames") or [])
        except (OSError, ValueError, TypeError):
            issues.append("历史增补收据损坏，无法确认应用过的音频来源。")
    current_is_original = bool(current and original and current == original)
    suspicious_receipt = (
        history["applied"]
        and language not in {"auto", history["audio_language"]}
    )
    if language == "auto":
        issues.append("主音频语言为 auto，无法确认当前实际语种；请明确选择后再增补。")
    if suspicious_receipt:
        issues.append(
            "历史增补记录指向英语主音频，与当前项目语言不符；"
            "现有合并 WAV 可能混入英语，不能仅靠文件头或日语字幕认定安全。"
        )
    if history["applied"] and not current_is_original:
        issues.append("当前工程使用增补后的来源，必须核对对应语音条目再重建。")
    if not original or not original.exists() or (original.is_symlink()):
        issues.append("无法确认原始日语语音包仍然可用；不得执行自动恢复。")
    if not _regular_file(original_index):
        issues.append("原始 quick_index.csv 不存在；无法直接回到最初的索引。")
    return {
        "read_only": True,
        "audio_language": language,
        "source_text_language": str(config.source_text_language),
        "current_source": str(current or ""),
        "current_source_exists": bool(current and current.exists()),
        "current_index": str(current_index or ""),
        "current_index_exists": bool(current_index and _regular_file(current_index)),
        "original_source_candidate": str(original or ""),
        "original_source_exists": bool(original and original.exists() and not original.is_symlink()),
        "original_source_fingerprint_recorded": bool(origin_fingerprint),
        "original_source_hash_verified_now": False,
        "original_index_candidate": str(original_index),
        "original_index_exists": _regular_file(original_index),
        "quick_scan_valid": scan_valid,
        "current_is_original": current_is_original,
        "legacy_apply": history,
        "suspicious_wrong_language_apply": suspicious_receipt,
        "requires_manual_review": bool(issues),
        "issues": issues,
        "remediation": (
            "保留当前工程与成品备份；核验原始日语语音包和 quick_index.csv，"
            "必要时在备份工程中恢复这两个来源并重新构建。"
            "本报告不执行恢复，不证明 WAV 实际发音语言。"
        ),
    }
