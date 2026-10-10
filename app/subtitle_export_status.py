"""Persistent, fail-closed provenance for derived ASS/SRT artifact health.

A successful text/time override commit and successful ASS/SRT generation are
distinct events. The receipt makes a failed/interrupted export detectable
after reload and validates both its source inputs and produced outputs.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .builder import atomic_write_text

EXPORT_RECEIPT_NAME = "subtitle_export_receipt.json"
INPUT_NAMES = (
    "manifest.json",
    "subtitles_overrides.json",
    "subtitle_timing_overrides.json",
)
SRT_NAME = "HSR_Voice_Archive.srt"
ASS_NAME = "HSR_Voice_Archive.ass"


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inputs(output_dir: Path) -> dict[str, str | None]:
    return {name: _sha256(output_dir / name) for name in INPUT_NAMES}


def record_subtitle_export(
    output_dir: Path,
    *,
    state: str,
    ass_required: bool,
    artifact_error: str = "",
) -> dict[str, Any]:
    if state not in {"pending", "failed", "current"}:
        raise ValueError("Invalid subtitle export state")
    if state == "current":
        if not (output_dir / SRT_NAME).is_file():
            raise ValueError("Cannot certify a missing SRT artifact")
        if ass_required and not (output_dir / ASS_NAME).is_file():
            raise ValueError("Cannot certify a missing ASS artifact")
    record = {
        "schema_version": 1,
        "state": state,
        "ass_required": bool(ass_required),
        "artifact_error": str(artifact_error or ""),
        "written_at": datetime.now(timezone.utc).isoformat(),
        "inputs": _inputs(output_dir),
        "outputs": {
            SRT_NAME: _sha256(output_dir / SRT_NAME) if state == "current" else None,
            ASS_NAME: _sha256(output_dir / ASS_NAME) if state == "current" and ass_required else None,
        },
    }
    atomic_write_text(
        output_dir / EXPORT_RECEIPT_NAME,
        json.dumps(record, ensure_ascii=False, indent=2),
    )
    return record


def read_subtitle_export_health(output_dir: Path | None) -> dict[str, Any]:
    if output_dir is None or not (output_dir / "manifest.json").is_file():
        return {"state": "not-built", "artifacts_current": False,
                "ass_required": False, "artifact_error": ""}
    file = output_dir / EXPORT_RECEIPT_NAME
    if not file.is_file():
        return {
            "state": "unverified", "artifacts_current": False,
            "ass_required": (output_dir / ASS_NAME).is_file(),
            "artifact_error": "No validated ASS/SRT export receipt exists for this build",
        }
    try:
        record = json.loads(file.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("schema_version") != 1
                or record.get("state") not in {"pending", "failed", "current"}
                or not isinstance(record.get("inputs"), dict)
                or not isinstance(record.get("outputs"), dict)):
            raise ValueError("Invalid subtitle artifact receipt schema")
        ass_required = bool(record.get("ass_required"))
        state = str(record["state"])
        error = str(record.get("artifact_error") or "")
        if record["inputs"] != _inputs(output_dir):
            state = "stale"
            error = "Subtitle source or human text/time overrides changed after the last export"
        elif state == "current":
            outputs = record["outputs"]
            current_srt = _sha256(output_dir / SRT_NAME)
            current_ass = _sha256(output_dir / ASS_NAME) if ass_required else None
            if (
                not current_srt or outputs.get(SRT_NAME) != current_srt
                or ass_required and (not current_ass or outputs.get(ASS_NAME) != current_ass)
            ):
                state = "stale"
                error = "A previously validated subtitle export is missing or changed"
        elif state == "pending":
            error = error or "Subtitle export was started but never certified as complete"
        elif state == "failed":
            error = error or "The last subtitle export failed"
        return {
            "state": state, "artifacts_current": state == "current",
            "ass_required": ass_required,
            "artifact_error": error,
            "written_at": str(record.get("written_at") or ""),
        }
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {
            "state": "unverified", "artifacts_current": False,
            "ass_required": (output_dir / ASS_NAME).is_file(),
            "artifact_error": f"Invalid or unreadable export receipt: {type(exc).__name__}",
        }
