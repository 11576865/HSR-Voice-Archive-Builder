"""Resolve one genuine project voice clip for an effects preview.

Only the server selects the media file from the active project. The browser
sends a cue ID, never a filesystem path or an invented audio duration.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .archive_bundle_lifecycle import OUTPUT, bundle_audio_source_status
from .project import ProjectConfig, resolve_project_path


def resolve_effect_voice(config: ProjectConfig, entry_id: object, *,
                         fade_ms: int) -> dict[str, object]:
    output = resolve_project_path(config, config.output_dir)
    if output is None:
        raise ValueError("No project output directory is configured")
    manifest = output / "manifest.json"
    if not manifest.is_file() or manifest.is_symlink():
        raise ValueError("项目尚未构建真实语音时间轴，不能提供有声特效预览")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Invalid project voice manifest")
    wanted = str(entry_id).strip()
    if not wanted:
        raise ValueError("必须选择真实项目字幕条目")
    index = next((
        i for i, row in enumerate(entries)
        if isinstance(row, dict) and wanted == str(
            row.get("index", row.get("id", row.get("filename", "")))
        )
    ), None)
    if index is None:
        raise ValueError("所选字幕不在当前项目语音时间轴中")
    row = entries[index]
    try:
        voice_start = float(row["start_seconds"])
        voice_end = float(row["audio_end_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("所选字幕缺少真实 PCM 语音起止时间") from exc
    if not all(math.isfinite(x) for x in (voice_start, voice_end)) or not (0 <= voice_start < voice_end):
        raise ValueError("所选字幕的真实语音时间无效")

    fade = max(0, int(fade_ms)) / 1000.0
    previous_end = 0.0
    next_start = None
    if index > 0:
        previous_end = float(entries[index - 1]["audio_end_seconds"])
    if index + 1 < len(entries):
        next_start = float(entries[index + 1]["start_seconds"])
    before_gap = max(0.0, voice_start - previous_end)
    # The final selected voice has no next cue: the ASS envelope *still* needs
    # its trailing Fade. A single-cue preview synthesizes video-only tail
    # frames, as the complete MKV publisher already does; speech PCM is never
    # stretched or padded to fabricate silence.
    after_gap = max(0.0, next_start - voice_end) if next_start is not None else None

    # For shared inter-cue silence a global shortest-gap budget guards the
    # author-chosen transition. This resolver only chooses the local envelope.
    before = min(fade, before_gap if index == 0 else before_gap / 2 if before_gap < fade * 2 else before_gap)
    after = (fade if after_gap is None
             else min(fade, after_gap / 2 if after_gap < fade * 2 else after_gap))
    if not config.subtitle_enable_kinetic:
        before = after = 0.0
    duration = before + voice_end - voice_start + after
    if duration > 120:
        raise ValueError("当前语音超过 120 秒的单条特效预览安全预算；不会静默截短，需选择较短语音")
    if duration < 0.01:
        raise ValueError("真实语音时长过短，无法生成可播放预览")
    source = bundle_audio_source_status(output)
    if not source["available"]:
        # Share precisely the same audio-provenance decision as inner ASS
        # export; a layout/style-only change cannot revoke a sealed MKV
        # whose exact artifact identity remains intact.
        raise ValueError(
            "所选项目语音暂不可用于动态预览：" + str(source["reason"])
            + "（当前项目输出目录：" + str(output) + "）"
        )
    audio = (output / "continuous.flac" if source["source"] == "independent-flac"
             else output / OUTPUT)
    return {
        "audio_source_path": audio,
        "audio_source_start_seconds": voice_start - before,
        "duration_seconds": duration,
        "voice_start_offset_seconds": before,
        "voice_duration_seconds": voice_end - voice_start,
        "preview_after_fade_ms": round(after * 1000),
        "voice_start_seconds": voice_start,
        "voice_end_seconds": voice_end,
    }
