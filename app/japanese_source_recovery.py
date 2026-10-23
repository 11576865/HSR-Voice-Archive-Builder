"""Safely reconstruct a separate Japanese source project from Quick Scan provenance.

Never rewrite the existing project, combined source, subtitles or MKV.
A recovered project is only a *candidate* until its own build and listening
acceptance succeed. No claim that audio language can be proven by WAV headers.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .game_profiles import game_index_url, normalize_language
from .project import (
    PROJECT_FILENAME, ProjectConfig, remember_project, resolve_project_path,
)
from .quick import source_inventory


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _original_members(index: Path) -> list[str]:
    if index.is_symlink() or not index.is_file():
        raise ValueError("初始索引缺失或为符号链接，拒绝恢复")
    members: list[str] = []
    with index.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            member = str(row.get("source_member_id") or row.get("filename") or "").replace("\\", "/").strip()
            filename = str(row.get("filename") or "").strip()
            parsed = PurePosixPath(member)
            if not member or parsed.is_absolute() or ".." in parsed.parts or filename != parsed.name:
                raise ValueError("初始索引含不可信的来源成员路径或文件名")
            if not member.lower().endswith(".wav"):
                raise ValueError("初始索引中存在非 WAV 的来源成员")
            members.append(member)
    if not members or len(set(members)) != len(members):
        raise ValueError("初始索引为空或包含重复来源成员，拒绝恢复")
    return members


def _verified_initial_source(config: ProjectConfig, report: Callable[[str], None] | None = None) -> tuple[Path, Path, str, int]:
    """Fully hash the primary archive and check the initial CSV enumerates exactly its WAVs."""
    root = Path(config.root).expanduser().resolve()
    if normalize_language(config.audio_language) != "ja" or normalize_language(config.source_text_language) != "ja":
        raise ValueError("仅可为主音频和源文本均明确设置为 ja 的工程创建日语恢复工程")
    if config.game_id != "honkai-star-rail":
        raise ValueError("此恢复器仅支持崩坏：星穹铁道日语语音工程")
    initial_scan = root / ".generated" / "quick_scan.json"
    initial_index = root / ".generated" / "quick_index.csv"
    if initial_scan.is_symlink() or not initial_scan.is_file():
        raise ValueError("原始 Quick Scan 记录缺失或不安全")
    try:
        scan = json.loads(initial_scan.read_text(encoding="utf-8"))
        if not isinstance(scan, dict) or normalize_language(scan.get("source_text_language")) != "ja":
            raise ValueError("原始 Quick Scan 没有记录 ja 源语言")
        primary = scan["english"]
        raw = str(primary["source"]).strip()
        recorded = str(primary["fingerprint"]["digest"]).strip().lower()
        if len(recorded) != 64 or any(c not in "0123456789abcdef" for c in recorded):
            raise ValueError("原始包指纹不存在或格式错误")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_symlink() or not candidate.exists():
            raise ValueError("原始来源已不存在或为符号链接")
        original = candidate.resolve()
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise ValueError("原始 Quick Scan 来源记录损坏") from exc
    if report:
        report("读取原始日语音频包并计算完整内容指纹")
    inventory = source_inventory(original)
    if str(inventory.get("fingerprint", {}).get("digest", "")).lower() != recorded:
        raise ValueError("原始日语音频包指纹不一致；拒绝恢复且不修改原工程")
    recorded_members = set(_original_members(initial_index))
    package_members = set(inventory["wav_members"])
    if recorded_members != package_members or len(recorded_members) != int(inventory["wav_count"]):
        raise ValueError(
            "初始索引与原始日语 WAV 成员不完全匹配；"
            f"索引={len(recorded_members)}, 音频={len(package_members)}，拒绝恢复"
        )
    if report:
        report(f"原始包指纹匹配，已核对 {len(package_members)} 条 WAV 与初始索引")
    return original, initial_index, recorded, len(package_members)


def create_isolated_japanese_recovery(
    config: ProjectConfig,
    *,
    report_progress: Callable[[str, str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Verify full source fingerprint and publish an isolated, unbuilt project.

    Recovery never mutates the existing HSR project. The only writes are a
    newly allocated sibling folder and the application's recent-project list.
    """
    root = Path(config.root).expanduser().resolve()
    project_config_path = root / PROJECT_FILENAME
    if not project_config_path.is_file() or project_config_path.is_symlink():
        raise ValueError("原工程配置不可读取")
    config_before = _sha256(project_config_path)
    try:
        on_disk = json.loads(project_config_path.read_text(encoding="utf-8"))
        if not isinstance(on_disk, dict) or on_disk.get("schema_version") != 1:
            raise ValueError("Invalid project config")
        for key in (
            "wav_source", "index_csv", "audio_language", "source_text_language",
            "game_id", "output_dir", "name",
        ):
            if str(on_disk.get(key, "")) != str(getattr(config, key)):
                raise ValueError(
                    "原工程配置自任务提交后发生变化，请重新核对并发起恢复"
                )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("原工程配置无法验证") from exc
    initial_index = root / ".generated" / "quick_index.csv"
    initial_index_before = _sha256(initial_index) if initial_index.is_file() and not initial_index.is_symlink() else ""

    def progress(message: str) -> None:
        if report_progress:
            report_progress("verify", message, 0, 1)

    original, initial_index, fingerprint, count = _verified_initial_source(config, progress)
    if not initial_index_before or _sha256(initial_index) != initial_index_before or _sha256(project_config_path) != config_before:
        raise ValueError("源工程配置或初始索引在核验过程中发生变化；拒绝发布恢复副本")

    # Use a new UUID-based sibling, never follow an existing destination.
    # Original audio stays referenced at its verified path rather than copied.
    candidate_name = f"{root.name}-JP-source-recovery-{uuid.uuid4().hex[:10]}"
    destination = root.parent / candidate_name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("恢复候选工程目标已存在")
    staging = Path(tempfile.mkdtemp(prefix=".hsr-jp-restore-stage-", dir=root.parent))
    published = False
    try:
        generated = staging / ".generated"
        generated.mkdir()
        shutil.copy2(initial_index, generated / "quick_index.csv")
        if _sha256(generated / "quick_index.csv") != initial_index_before:
            raise ValueError("初始索引在创建恢复副本时发生变化")
        # Do not transplant generated incremental WAV caches, source versions,
        # previous outputs, subtitle overrides, checkpoint or old remote plan.
        recovered = replace(
            config,
            name=f"{config.name} · 日语原始包恢复候选",
            root=str(destination),
            wav_source=str(original),
            index_csv=".generated/quick_index.csv",
            output_dir="output",
            state_dir=".state",
            bilingual_csv="",
            chs_source="",
            reference_source="",
            glossary_path="",
            update_candidates="",
            remote_index_url=game_index_url("honkai-star-rail", "ja"),
            wav_source_fingerprint=fingerprint,
            chs_source_fingerprint="",
            reference_source_fingerprint="",
            reference_text_embedded=False,
            managed_project_root=True,
            audio_language="ja",
            source_text_language="ja",
        )
        (staging / PROJECT_FILENAME).write_text(
            json.dumps(asdict(recovered), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if _sha256(project_config_path) != config_before or _sha256(initial_index) != initial_index_before:
            raise ValueError("原工程在发布前发生变化；拒绝发布恢复副本")
        os.replace(staging, destination)
        published = True
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)

    # The source project's files/metadata are never rewritten.
    try:
        remember_project(destination)
    except (OSError, ValueError):
        # Candidate was atomically published; a failure to update recents is
        # not an excuse to delete the successful recovery folder.
        pass
    if report_progress:
        report_progress("published", "独立日语来源恢复候选已创建，尚未构建或试听验收", 1, 1)
    return {
        "candidate_project": str(destination),
        "original_project": str(root),
        "source": str(original),
        "source_fingerprint_verified": True,
        "initial_index_verified": True,
        "wav_members": count,
        "isolated": True,
        "built": False,
        "listening_verified": False,
        "original_project_modified": False,
        "next_action": "打开新恢复候选项目，独立构建并试听核验；原工程及其输出保持原状。",
    }
