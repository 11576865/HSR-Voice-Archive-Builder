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
from .delivery_artifacts import prepare_complete_sources, seal_complete_delivery
from .archive_bundle_lifecycle import (
    _sources, bundle_export_status, load_previous_bundle_image,
    previous_bundle_settings,
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


def _input_identity(output: Path, *, include_fonts: bool, settings: dict) -> str:
    evidence = {
        "inputs": _sources(output, include_fonts),
        "background": settings["background_spec"],
        "background_color": settings["background_color"],
        "attach_fonts": include_fonts,
    }
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


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
    if not status["needs_rebuild"] or not status["can_generate"]:
        return None
    settings = previous_bundle_settings(output)
    key = str(config.root)
    snapshot = _input_identity(
        output, include_fonts=settings["attach_project_fonts"], settings=settings,
    )
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
