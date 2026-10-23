"""Fail-closed source locale contract for remote voice additions.

The index and Hugging Face WAV resolver are separate inputs. Neither a
historical EN.xlsx default nor a cached update plan may override the project's
explicit primary audio language.
"""
from __future__ import annotations

import json
from pathlib import Path

from .game_profiles import game_index_url, game_provider, normalize_language
from .project import ProjectConfig


SUPPORTED_INCREMENTAL_LANGUAGES = frozenset({"en", "ja", "ko", "zh-CN"})


def require_incremental_audio_language(config: ProjectConfig) -> str:
    """Require a declared source locale before adding real audio to an archive."""
    raw_audio = str(config.audio_language or "").strip()
    if not raw_audio or raw_audio.casefold() == "auto":
        raise ValueError(
            "增补真实语音前必须在项目设置中明确选择主音频语言（如 ja）；"
            "auto 无法证明原始 WAV 的语种，已拒绝自动下载。"
        )
    language = normalize_language(raw_audio)
    if language not in SUPPORTED_INCREMENTAL_LANGUAGES:
        raise ValueError(f"该主音频语言暂不支持远程自动增补：{raw_audio}")
    source_text = normalize_language(config.source_text_language)
    if source_text != language:
        raise ValueError(
            "当前增补索引要求主音频语言与主音频对应文本语言一致："
            f"audio={language}, source_text={source_text}。"
            "请先核对项目设置，不能据英语文本索引替日语语音下载英语 WAV。"
        )
    return language


def select_incremental_index_url(config: ProjectConfig, requested_url: str = "") -> str:
    """Correct stale built-in EN defaults without clobbering a deliberate custom URL."""
    language = require_incremental_audio_language(config)
    canonical = game_index_url(config.game_id, language)
    configured = str(requested_url or "").strip() or str(config.remote_index_url or "").strip()
    known_indexes = set(game_provider(config.game_id).get("index_urls", {}).values())
    if not configured or configured in known_indexes:
        return canonical
    return configured


def require_matching_update_plan(plan: dict, audio_language: str) -> None:
    """Old plans lack locale provenance; force a fresh check before any download."""
    planned = normalize_language(str(plan.get("audio_language") or ""))
    if not plan.get("audio_language") or planned != audio_language:
        raise ValueError(
            "增补计划的音频语种缺失或已改变；请重新检查远程增补，"
            f"当前主音频语言={audio_language}，计划语言={plan.get('audio_language')!r}。"
        )


def require_safe_prior_incremental_apply(report_path: Path, audio_language: str) -> None:
    """Do not treat a pre-locale-guard update as proof of correct WAV language.

    This is intentionally non-destructive. The affected project must first be
    reconstructed from a trustworthy original source package.
    """
    if not report_path.is_file():
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("历史增补收据不可读取，必须先检查归档来源，禁止继续混入音频") from exc
    if not isinstance(report, dict):
        raise ValueError("历史增补收据无效，已拒绝继续自动增补")
    if report.get("project_updated") and report.get("audio_language") != audio_language:
        raise ValueError(
            "发现未记录正确语种或与当前语种不同的历史增补。此前可能已将英语 WAV 合入"
            "日语来源；请保留原始材料、从可信原始音频包恢复工程并重新检查增补，"
            "程序不会自动删除或覆盖现有音频。"
        )
