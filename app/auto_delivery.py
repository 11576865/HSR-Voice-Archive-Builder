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
from pathlib import Path

from .archive_bundle_backgrounds import RESOLUTIONS
from .archive_bundle_exporter import ArchiveBundleExporter
from .archive_bundle_lifecycle import (
    _sources, bundle_export_status, load_previous_bundle_image,
    previous_bundle_settings,
)
from .jobs import create_job
from .project import ProjectConfig, resolve_project_path
from .project_fonts import font_asset_directory

_LOCK = threading.Lock()
_ATTEMPTS: dict[str, str] = {}


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
        if _ATTEMPTS.get(key) == snapshot:
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
        _ATTEMPTS[key] = snapshot
        if len(_ATTEMPTS) > 128:
            _ATTEMPTS.pop(next(iter(_ATTEMPTS)))
        return job.id
