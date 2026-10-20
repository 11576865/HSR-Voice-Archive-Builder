"""Deduplicated, source-aware background rebuild of the current playback bundle.

An active web UI polls /api/status. This reconciles source changes after a
completed build or in-app subtitle edit without re-encoding the lossless FLAC.
Never queue if subtitle provenance or a same-project writer is not idle.
External unvalidated ASS/SRT edits deliberately remain stale until verified.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from .archive_bundle_backgrounds import RESOLUTIONS
from .archive_bundle_exporter import ArchiveBundleExporter
from .delivery_artifacts import (
    prepare_complete_sources, seal_complete_delivery,
    verify_internal_ass_audio_source,
)
from .archive_bundle_lifecycle import (
    _read_receipt, _sources, bundle_export_status, bundle_audio_source_status,
    load_previous_bundle_image, previous_bundle_settings,
)
from .jobs import create_job, get_job
from .project import ProjectConfig, resolve_project_path
from .project_fonts import font_asset_directory

_LOCK = threading.Lock()
# source fingerprint, job id, last attempt monotonic time, consecutive failures.
# A failed job must not permanently poison the source generation, nor may
# frequent UI polls create an expensive infinite FFmpeg retry loop.
_ATTEMPTS: dict[str, tuple[str, str, float, int]] = {}
RETRY_BASE_SECONDS = 60.0
RETRY_MAX_SECONDS = 3600.0


def _input_identity(output: Path, *, include_fonts: bool, settings: dict,
                    include_chapter_flac: bool = False) -> str:
    evidence = {
        "inputs": _sources(output, include_fonts, include_chapter_flac),
        "chapters": include_chapter_flac,
        "background": settings["background_spec"],
        "background_color": settings["background_color"],
        "attach_fonts": include_fonts,
    }
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()



def publish_generated_ass_complete_mkv(
    config: ProjectConfig, output: Path, report_progress,
) -> dict[str, object]:
    """Finish an explicit ASS regeneration by publishing its final MKV.

    Called within the already-serialized ass-export job. Never report success
    for a loose source .ass while the previous MKV still carries old subtitles.
    ArchiveBundleExporter verifies staged streams and atomically replaces MKV;
    seal_complete_delivery removes the temporary working ASS/SRT/FLAC only
    AFTER the replacement passes its source-receipt checks.
    """
    if config.delivery_mode != "complete":
        raise ValueError("Complete MKV publication requires complete delivery mode")
    status = bundle_export_status(output)
    if not status["can_generate"]:
        raise ValueError(
            "新字幕尚不能安全封装进 MKV：" + "；".join(status["blockers"])
        )
    settings = previous_bundle_settings(output)
    spec = settings["background_spec"]
    if spec["mode"] == "image" and not settings["image_saved"]:
        raise ValueError("已保存的 MKV 图片背景不可用，旧 MKV 保持不变")
    chapter_enabled = bool(getattr(config, "generate_chapter_flac", False))
    if chapter_enabled and not (
        output / "chapter_flac" / "chapter_flac_manifest.json"
    ).is_file():
        raise ValueError("章节 FLAC 缺失，拒绝替换现有 MKV")
    from .subtitles import subtitle_render_config
    report_progress("prepare-bundle", "正在准备 MKV 的已有音视频来源", 0, 0)
    prepare_complete_sources(
        output,
        source_language=config.source_text_language or "en",
        target_language=config.target_language or "zh-CN",
        render_config=subtitle_render_config(config),
    )
    background = load_previous_bundle_image(output) if spec["mode"] == "image" else None
    width, height = RESOLUTIONS[spec["resolution"]]
    result = ArchiveBundleExporter(width=width, height=height).export(
        output / "continuous.flac",
        output / "HSR_Voice_Archive.ass",
        output / "HSR_Voice_Archive_Bundle.mkv",
        fonts_dir=font_asset_directory(output),
        include_fonts=settings["attach_project_fonts"],
        background_color=settings["background_color"],
        background_image=background, background_spec=spec,
        include_chapter_flac=chapter_enabled,
        require_current_ass=True,
        progress=lambda current, total, message: report_progress(
            "encode-bundle", message, current, total,
        ),
    )
    report_progress("verify-bundle", "正在封存已校验的 MKV 新字幕轨", 0, 0)
    seal_complete_delivery(output)
    final = bundle_export_status(output)
    if final["state"] != "current":
        raise RuntimeError("MKV 更新后未达到已验证状态：" + final["message"])
    return result



def queue_ass_regeneration(config: ProjectConfig, output: Path):
    """One progress-visible job for every explicit or auto ASS regeneration.

    In complete mode, success means that the ASS has been embedded into the
    verified current MKV; an intermediate loose ASS is never a success signal.
    """
    from .subtitles import (
        refresh_subtitle_artifacts_from_settings, subtitle_render_config,
    )

    def run(report_progress):
        counts = {"total": 0}

        def on_cue(current: int, total: int) -> None:
            counts["total"] = total
            report_progress(
                "render", f"正在生成 ASS 字幕：{current}/{total} 条", current, total,
            )

        # Check the lossless audio source before writing a pending receipt or
        # regenerated ASS/SRT. Previously this failed after subtitle mutation,
        # leaving an avoidable stale intermediate when the archive had never
        # produced a verifiable FLAC/MKV source.
        if config.delivery_mode == "complete":
            report_progress("preflight-audio", "正在完整解码核验 FLAC 音轨，长档案可能需要几分钟", 0, 0)
            try:
                verify_internal_ass_audio_source(output)
            except ValueError as exc:
                raise ValueError(
                    "生成 ASS（内）被阻止：" + str(exc)
                    + "；旧 ASS/SRT/MKV 均未因本次失败而改写。"
                    + "仅需独立字幕时可使用「生成 ASS（外）」。"
                ) from exc
        report_progress("prepare", "正在读取字幕并准备 ASS", 0, 0)
        result = refresh_subtitle_artifacts_from_settings(
            output,
            source_language=config.source_text_language or "en",
            target_language=config.target_language or "zh-CN",
            generate_ass=True,
            render_config=subtitle_render_config(config),
            ass_progress=on_cue,
        )
        if result.get("ass_error"):
            raise ValueError("ASS 生成失败，MKV 未更新：" + str(result["ass_error"]))
        done = max(counts["total"], 1)
        if config.delivery_mode == "complete":
            report_progress(
                "render", "新 ASS 已生成，正在更新 MKV 内的字幕轨道", done, done,
            )
            bundle = publish_generated_ass_complete_mkv(
                config, output, report_progress,
            )
            return {
                **result, "bundle": bundle, "delivery": "complete-mkv",
                "output": bundle.get("output"),
            }
        report_progress("render", "独立 ASS 字幕已生成", done, done)
        return result

    return create_job(
        "ass-export", run, with_progress=True,
        project_root=config.root, project_name=config.name,
    )



def queue_external_ass_export(config: ProjectConfig, output: Path):
    """Explicit *standalone* export; never publishes or invalidates MKV."""
    from .subtitles import export_independent_ass

    def run(report_progress):
        report_progress("prepare", "正在准备独立 ASS 副本", 0, 0)
        def progress(current: int, total: int) -> None:
            report_progress("render", f"正在生成外部 ASS：{current}/{total} 条", current, total)
        result = export_independent_ass(config, output, ass_progress=progress)
        report_progress("complete", "独立 ASS 已另存；原 ASS 和 MKV 未改变", 1, 1)
        return result

    return create_job(
        "ass-external", run, with_progress=True,
        project_root=config.root, project_name=config.name,
    )


def maybe_queue_complete_mkv(config: ProjectConfig) -> str | None:
    """Enqueue at most once for each changed certified source generation.

    This is reconciled when the local UI refreshes its project status.
    The build pipeline itself directly publishes the first complete bundle.
    """
    if config.delivery_mode != "complete":
        return None
    output = resolve_project_path(config, config.output_dir)
    if output is None:
        return None
    status = bundle_export_status(output)
    receipt = _read_receipt(output)
    chapter_enabled = bool(getattr(config, "generate_chapter_flac", False))
    chapter_mode_changed = bool(receipt and receipt.get("include_chapter_flac")) != chapter_enabled
    if (not status["needs_rebuild"] and not chapter_mode_changed) or not status["can_generate"]:
        return None
    if chapter_enabled and not (output / "chapter_flac" / "chapter_flac_manifest.json").is_file():
        return None
    settings = previous_bundle_settings(output)
    if settings.get("background_spec", {}).get("mode") == "image" and not settings.get("image_saved", False):
        return None
    key = str(config.root)
    try:
        snapshot = _input_identity(
            output, include_fonts=settings["attach_project_fonts"], settings=settings,
            include_chapter_flac=chapter_enabled,
        )
    except (OSError, ValueError, TypeError):
        # Invalid chapter sources must not erase the whole project dashboard.
        return None
    with _LOCK:
        prior = _ATTEMPTS.get(key)
        failures = 0
        if prior is not None and prior[0] == snapshot:
            _, prior_job_id, last_attempt, failures = prior
            job_status = get_job(prior_job_id)
            state = (job_status or {}).get("state")
            if state in {"queued", "running", "succeeded", "awaiting_input"}:
                return None
            # Job journals can be pruned, but the same source must not be
            # retried every few seconds while a UI is polling /api/status.
            failures += 1
            delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** min(failures - 1, 6)))
            if time.monotonic() - last_attempt < delay:
                return None

        def run(report_progress):
            # Revalidate immediately before launching an expensive encoding job.
            current = bundle_export_status(output)
            if not current["can_generate"]:
                raise RuntimeError("自动 MKV 更新的 FLAC/SRT/ASS 来源尚未通过校验")
            now = previous_bundle_settings(output)
            if _input_identity(
                output, include_fonts=now["attach_project_fonts"], settings=now,
                include_chapter_flac=chapter_enabled,
            ) != snapshot:
                raise RuntimeError("自动 MKV 更新的输入已变化，等待下一轮任务检查")
            from .subtitles import subtitle_render_config
            prepare_complete_sources(
                output, source_language=config.source_text_language,
                target_language=config.target_language,
                render_config=subtitle_render_config(config),
            )
            spec = now["background_spec"]
            background = (
                load_previous_bundle_image(output) if spec["mode"] == "image"
                else None
            )
            width, height = RESOLUTIONS[spec["resolution"]]
            result = ArchiveBundleExporter(width=width, height=height).export(
                output / "continuous.flac", output / "HSR_Voice_Archive.ass",
                output / "HSR_Voice_Archive_Bundle.mkv",
                fonts_dir=font_asset_directory(output),
                include_fonts=now["attach_project_fonts"],
                background_color=now["background_color"],
                background_image=background, background_spec=spec,
                include_chapter_flac=chapter_enabled,
                require_current_ass=True,
                progress=lambda done, total, message: report_progress(
                    "encode", message, done, total,
                ),
            )
            seal_complete_delivery(output)
            return result

        # Job registry enforces single-writer ownership. If another job is
        # active, don't consume the input generation: retry on next status.
        try:
            job = create_job(
                "archive-bundle-export", run, with_progress=True,
                project_root=config.root, project_name=config.name,
            )
        except RuntimeError:
            return None
        _ATTEMPTS[key] = (snapshot, job.id, time.monotonic(), failures)
        if len(_ATTEMPTS) > 128:
            _ATTEMPTS.pop(next(iter(_ATTEMPTS)))
        return job.id
