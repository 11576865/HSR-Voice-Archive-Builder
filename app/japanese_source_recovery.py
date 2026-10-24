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

RECOVERY_ATTESTATION = ".generated/japanese_source_attestation.json"


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
        # The original is deliberately NOT copied. Its fingerprint must be
        # rechecked by each subsequent build because the source lives outside
        # this candidate and can be replaced after candidate creation.
        (staging / RECOVERY_ATTESTATION).write_text(json.dumps({
            "schema_version": 1,
            "kind": "isolated-ja-source-recovery",
            "source": str(original),
            "source_fingerprint": fingerprint,
            "index_sha256": initial_index_before,
            "wav_members": count,
            "audio_language": "ja",
            "source_text_language": "ja",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
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
            recovery_source_attestation_required=True,
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
        "build_source_attestation_required": True,
        "wav_members": count,
        "isolated": True,
        "built": False,
        "listening_verified": False,
        "original_project_modified": False,
        "next_action": "打开新恢复候选项目，独立构建并试听核验；原工程及其输出保持原状。",
    }


def verify_japanese_recovery_build_inputs(config: ProjectConfig) -> dict[str, Any] | None:
    """Guard the *next* build against source changes after recovery creation.

    Old projects without a recovery receipt retain their historical build flow.
    The check is intentionally performed inside the build job, not when an HTTP
    request starts, so queued jobs do not trust a stale preflight.
    """
    root = Path(config.root).expanduser().resolve()
    proof_path = root / RECOVERY_ATTESTATION
    if not proof_path.exists():
        if config.recovery_source_attestation_required:
            raise ValueError("日语恢复工程缺少必须的来源证明文件，拒绝构建")
        # Candidates created by the older release lacked build-time proofs.
        # Never silently treat an old generated candidate as an ordinary
        # project merely because its receipt does not exist.
        import re
        if re.fullmatch(r".+-JP-source-recovery-[0-9a-f]{10}", root.name):
            raise ValueError(
                "旧版日语恢复候选缺少构建时来源证明；请从仍可核验的"
                "原始日语包重新创建独立恢复候选，旧工程保持不变"
            )
        return None
    if not config.recovery_source_attestation_required:
        raise ValueError("存在日语恢复证明文件但工程未标记需要来源验证，拒绝构建")
    if proof_path.is_symlink() or not proof_path.is_file():
        raise ValueError("日语恢复来源证明文件不是安全的常规文件")
    try:
        receipt = json.loads(proof_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("日语恢复来源证明文件无效，拒绝构建") from exc
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1 or receipt.get("kind") != "isolated-ja-source-recovery":
        raise ValueError("日语恢复来源证明格式无效，拒绝构建")
    if config.audio_language != "ja" or config.source_text_language != "ja" or config.game_id != "honkai-star-rail":
        raise ValueError("日语恢复工程主音频、源文本或游戏类型发生变化，拒绝构建")
    original_path = resolve_project_path(config, config.wav_source)
    index_path = resolve_project_path(config, config.index_csv)
    if original_path is None or index_path is None:
        raise ValueError("恢复工程音频或索引路径缺失")
    expected_source = Path(str(receipt.get("source") or "")).expanduser()
    if (
        not expected_source.is_absolute() or original_path.is_symlink()
        or not original_path.exists() or original_path.resolve() != expected_source
        or str(config.index_csv).replace("\\", "/") != ".generated/quick_index.csv"
        or index_path.is_symlink() or not index_path.is_file()
    ):
        raise ValueError("恢复候选已偏离经过核验的原始日语来源或初始索引，拒绝构建")
    recorded_fingerprint = str(receipt.get("source_fingerprint") or "")
    recorded_index = str(receipt.get("index_sha256") or "")
    if (
        len(recorded_fingerprint) != 64
        or len(recorded_index) != 64
        or not all(c in "0123456789abcdef" for c in recorded_fingerprint + recorded_index)
        or recorded_fingerprint != str(config.wav_source_fingerprint)
        or _sha256(index_path) != recorded_index
    ):
        raise ValueError("恢复工程初始索引或来源指纹证明不一致，拒绝构建")
    inventory = source_inventory(original_path)
    if inventory["fingerprint"]["digest"] != recorded_fingerprint:
        raise ValueError("恢复工程所引用的原始语音包自创建后已发生变化，拒绝构建")
    members = _original_members(index_path)
    recorded_count = receipt.get("wav_members")
    if (
        type(recorded_count) is not int or recorded_count <= 0
        or len(members) != recorded_count
        or set(members) != set(inventory["wav_members"])
    ):
        raise ValueError("恢复工程初始索引的来源成员集合不完整，拒绝构建")
    return {
        "source": str(original_path),
        "source_fingerprint": recorded_fingerprint,
        "index_sha256": recorded_index,
        "wav_members": recorded_count,
        "member_ids": members,
    }


def verify_japanese_recovery_build_manifest(
    config: ProjectConfig,
    initial: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Re-attest all members and produced manifest; do not claim speech QA."""
    if initial is None:
        return None
    checked = verify_japanese_recovery_build_inputs(config)
    if checked is None or checked["source_fingerprint"] != initial["source_fingerprint"]:
        raise ValueError("构建期间原始日语语音包来源证明消失或改变")
    output = resolve_project_path(config, config.output_dir)
    if output is None:
        raise ValueError("恢复工程输出路径缺失")
    manifest = output / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("恢复候选构建后缺少安全的音频成员清单")
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
        entries = document["entries"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("恢复候选的成品成员清单损坏") from exc
    if not isinstance(entries, list) or len(entries) != initial["wav_members"]:
        raise ValueError("恢复构建音频成员数与原始日语包不一致")
    built: list[str] = []
    for row in entries:
        if not isinstance(row, dict):
            raise ValueError("恢复构建音频成员清单含无效行")
        name = str(row.get("source_member_id") or row.get("filename") or "")
        if not name or str(row.get("filename") or "") != PurePosixPath(name).name:
            raise ValueError("恢复构建音频成员的文件身份已发生变化")
        built.append(name)
    if len(set(built)) != len(built) or set(built) != set(initial["member_ids"]):
        raise ValueError("恢复构建的章节成员身份与原始日语包不完全匹配")
    return {
        "status": "source-members-verified",
        "source_fingerprint": initial["source_fingerprint"],
        "wav_members": len(built),
        "chapter_member_identity_verified": True,
        "spoken_japanese_verified": False,
        "human_listening_required": True,
    }
