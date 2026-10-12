"""Evidence for actual exported ASS bytes, independent of unsaved preview state.

The export receipt, original ASS bytes, project style policy and resulting
raster sample are bound by hashes. Evidence is deliberately *sampled*, never
a certificate of the whole project timeline.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .ass_preview import _render_raw_rgb
from .builder import atomic_write_text
from .project import ProjectConfig, load_project, resolve_project_path
from .subtitle_export_status import (
    ASS_NAME, EXPORT_RECEIPT_NAME, _sha256, read_subtitle_export_health,
    export_render_policy_sha256,
)
from subtitle_layout.rendered_ink import visible_ink_bbox
from subtitle_layout.safe_area import SafeArea

PROOF_NAME = "ass_render_evidence.json"
MAX_ASS_BYTES = 64 * 1024 * 1024
MAX_ASS_DIALOGUES = 200000
MAX_EXPORT_SAMPLES = 12


def style_policy_sha256(config: ProjectConfig) -> str:
    from dataclasses import asdict
    values = asdict(config)
    relevant = {
        k: v for k, v in values.items()
        if k.startswith("subtitle_") or k in {"source_text_language", "target_language"}
    }
    canonical = json.dumps(relevant, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_time(value: str) -> float:
    parts = value.strip().split(":")
    if len(parts) != 3:
        raise ValueError("Malformed ASS cue time")
    hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError("ASS cue time outside expected bounds")
    total = hours * 3600 + minutes * 60 + seconds
    if not math.isfinite(total):
        raise ValueError("Nonfinite ASS cue time")
    return total


def plan_export_ass_samples(ass_text: str, sample_limit: int = 8) -> dict[str, Any]:
    if type(sample_limit) is not int or not 1 <= sample_limit <= MAX_EXPORT_SAMPLES:
        raise ValueError("ASS sample limit must be an integer from 1 to 12")
    rows: list[tuple[float, float]] = []
    in_events = False
    for line in ass_text.splitlines():
        value = line.strip()
        if value.startswith("[") and value.endswith("]"):
            in_events = value.lower() == "[events]"
            continue
        if not in_events or not value.startswith("Dialogue:"):
            continue
        parts = value[len("Dialogue:"):].strip().split(",", 9)
        if len(parts) < 10:
            raise ValueError("Malformed ASS dialogue record")
        start, end = _parse_time(parts[1]), _parse_time(parts[2])
        if end <= start:
            raise ValueError("Non-positive ASS dialogue duration")
        rows.append((start, end))
        if len(rows) > MAX_ASS_DIALOGUES:
            raise ValueError("ASS subtitle cue count exceeds bounded evidence limit")
    if not rows:
        raise ValueError("ASS has no dialogue events to verify")
    unique = sorted(set(rows))
    count = min(len(unique), sample_limit)
    indices = sorted({round(i * (len(unique) - 1) / (count - 1))
                      for i in range(count)}) if count > 1 else [len(unique) // 2]
    chosen = []
    for index in indices:
        start, end = unique[index]
        chosen.append({
            "dialogue_start_seconds": round(start, 3),
            "dialogue_end_seconds": round(end, 3),
            "timestamp_seconds": round(start + (end - start) * 0.5, 3),
        })
    return {
        "dialogue_count": len(rows),
        "unique_window_count": len(unique),
        "sample_limit": sample_limit,
        "sample_count": len(chosen),
        "unexamined_unique_windows": len(unique) - len(chosen),
        "samples": chosen,
    }


def _export_identity(output_dir: Path, config: ProjectConfig) -> dict[str, str]:
    health = read_subtitle_export_health(output_dir)
    if health.get("state") != "current" or not health.get("ass_required"):
        raise ValueError("ASS export receipt is not current: " + str(health.get("state")))
    ass = output_dir / ASS_NAME
    receipt = output_dir / EXPORT_RECEIPT_NAME
    if ass.is_symlink() or receipt.is_symlink():
        raise ValueError("ASS evidence rejects symlinked artifact or receipt")
    if not ass.is_file() or ass.stat().st_size > MAX_ASS_BYTES:
        raise ValueError("Missing or oversized exported ASS artifact")
    try:
        receipt_data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Unreadable ASS export receipt") from exc
    from .subtitles import subtitle_render_config
    actual = export_render_policy_sha256(
        subtitle_render_config(config),
        config.source_text_language or "en",
        config.target_language or "zh-CN",
    )
    if not isinstance(receipt_data, dict) or receipt_data.get("render_policy_sha256") != actual:
        raise ValueError("ASS render policy is unverified or outdated; regenerate ASS with saved settings")
    return {
        "ass_sha256": _sha256(ass) or "",
        "export_receipt_sha256": _sha256(receipt) or "",
        "style_policy_sha256": style_policy_sha256(config),
    }


def read_exported_ass_evidence(
    output_dir: Path | None, config: ProjectConfig | None = None,
) -> dict[str, Any]:
    if output_dir is None or config is None:
        return {"state": "not-built", "verified": False}
    path = output_dir / PROOF_NAME
    if not path.is_file():
        return {"state": "unverified", "verified": False}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("schema_version") != 1
                or record.get("scope") != "bounded_exported_ass_samples"
                or record.get("full_interval_verified") is not False):
            raise ValueError("Unknown rendered evidence schema")
        current = _export_identity(output_dir, config)
        if any(record.get(field) != hashvalue for field, hashvalue in current.items()):
            return {"state": "stale", "verified": False,
                    "reason": "Exported ASS, receipt or style policy changed"}
        status = record.get("status")
        if status not in {"sampled_violation", "sampled_no_violation", "no_visible_evidence"}:
            raise ValueError("Invalid evidence status")
        return {"state": status, "verified": True, "evidence": record}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"state": "stale", "verified": False,
                "reason": f"Exported ASS evidence unavailable: {type(exc).__name__}"}


def verify_exported_ass(
    config: ProjectConfig, output_dir: Path, *,
    cancel_event: threading.Event | None = None,
    report_progress: Callable[[str, str, int, int], None] | None = None,
    sample_limit: int = 8,
) -> dict[str, Any]:
    if cancel_event is None:
        cancel_event = threading.Event()
    deadline = time.monotonic() + 120.0
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Exported ASS evidence requires FFmpeg with libass")
    identity = _export_identity(output_dir, config)
    original = output_dir / ASS_NAME
    with tempfile.TemporaryDirectory(prefix="hsr-exported-ass-proof-") as td:
        snap = Path(td) / "export.ass"
        # Snapshot the exported bytes before sampling, not unsaved trial text.
        shutil.copyfile(original, snap)
        if _sha256(snap) != identity["ass_sha256"]:
            raise RuntimeError("Exported ASS changed while acquiring the verification snapshot")
        plan = plan_export_ass_samples(snap.read_text(encoding="utf-8-sig"), sample_limit)
        safe = SafeArea(
            margin_left_percent=config.subtitle_margin_horizontal_percent,
            margin_right_percent=config.subtitle_margin_horizontal_percent,
            margin_top_percent=config.subtitle_margin_vertical_percent,
            margin_bottom_percent=config.subtitle_margin_vertical_percent,
        )
        safe_bounds = {
            "x_min": safe.x_min, "x_max": safe.x_max,
            "y_min": safe.y_min, "y_max": safe.y_max,
        }
        checked = []
        for index, sample in enumerate(plan["samples"]):
            if cancel_event.is_set():
                raise InterruptedError("Exported ASS verification canceled")
            if time.monotonic() >= deadline:
                raise TimeoutError("Exported ASS verification exceeded the overall deadline")
            stamp = sample["timestamp_seconds"]
            if report_progress is not None:
                report_progress("libass", f"正在检查已导出 ASS 的第 {index+1}/{plan['sample_count']} 帧",
                                index, plan["sample_count"])
            black = _render_raw_rgb(
                ffmpeg, snap, timestamp=stamp, background="black",
                cancel_event=cancel_event,
                timeout_seconds=max(0.25, min(12.0, deadline - time.monotonic())),
            )
            if cancel_event.is_set():
                raise InterruptedError("Exported ASS verification canceled")
            if time.monotonic() >= deadline:
                raise TimeoutError("Exported ASS verification exceeded the overall deadline")
            white = _render_raw_rgb(
                ffmpeg, snap, timestamp=stamp, background="white",
                cancel_event=cancel_event,
                timeout_seconds=max(0.25, min(12.0, deadline - time.monotonic())),
            )
            bbox = visible_ink_bbox(black, white, 1920, 1080, threshold=16)
            status = ("no_visible_ink" if bbox is None else
                      "inside_safe_area" if (
                          safe_bounds["x_min"] <= bbox[0] and bbox[2] <= safe_bounds["x_max"]
                          and safe_bounds["y_min"] <= bbox[1] and bbox[3] <= safe_bounds["y_max"]
                      ) else "outside_safe_area")
            checked.append({**sample, "bbox": list(bbox) if bbox else None,
                            "status": status})
    if cancel_event.is_set():
        raise InterruptedError("Exported ASS verification canceled")
    latest = load_project(Path(config.root))
    latest_output = resolve_project_path(latest, latest.output_dir)
    if (latest_output is None or latest_output.resolve() != output_dir.resolve()
            or _export_identity(output_dir, latest) != identity):
        raise RuntimeError("Exported ASS identity or current project policy changed during verification")
    violation = sum(item["status"] == "outside_safe_area" for item in checked)
    invisible = sum(item["status"] == "no_visible_ink" for item in checked)
    status = ("sampled_violation" if violation else
              "no_visible_evidence" if invisible == len(checked)
              else "sampled_no_violation")
    evidence = {
        "schema_version": 1,
        "scope": "bounded_exported_ass_samples",
        "full_interval_verified": False,
        "renderer": "ffmpeg-libass",
        "threshold_rgb": 16,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        **identity,
        "safe_area": safe_bounds,
        "dialogue_count": plan["dialogue_count"],
        "unique_window_count": plan["unique_window_count"],
        "unexamined_unique_windows": plan["unexamined_unique_windows"],
        "sample_limit": plan["sample_limit"],
        "sample_count": len(checked),
        "violation_count": violation,
        "no_visible_ink_count": invisible,
        "status": status,
        "samples": checked,
    }
    if cancel_event.is_set():
        raise InterruptedError("Exported ASS verification canceled")
    atomic_write_text(output_dir / PROOF_NAME,
                      json.dumps(evidence, ensure_ascii=False, indent=2))
    if report_progress is not None:
        report_progress("complete", "已导出 ASS 抽样证据已关联原始文件哈希", len(checked), len(checked))
    return evidence
