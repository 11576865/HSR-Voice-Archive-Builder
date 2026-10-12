"""Bounded, resumable, artifact-bound exported-ASS window-pass checkpoints.

Completing every window's sampled midpoint/effect NEVER verifies all timeline
instants. Each batch commits atomically only after all its frames render.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from subtitle_layout.rendered_ink import visible_ink_bbox
from subtitle_layout.safe_area import SafeArea

from .ass_export_sampling import plan_export_ass_window_batch
from .ass_preview import _render_raw_rgb
from .ass_artifact_evidence import ASS_NAME, _export_identity
from .builder import atomic_write_text
from .project import ProjectConfig, load_project, resolve_project_path
from .subtitle_export_status import _sha256

COVERAGE_FILE = "ass_render_coverage_checkpoint.json"
RECEIPTS_DIR = "ass_render_coverage_batches"
VERSION = 1
ALGORITHM = "sequential-ass-window-pass-v1"
MAX_BATCH_RECEIPT_BYTES = 256 * 1024
MAX_CHECKPOINT_BYTES = 1024 * 1024


def _receipt_path(output_dir: Path, run_id: str, batch_id: int) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Invalid coverage run identity")
    return output_dir / RECEIPTS_DIR / f"{run_id}-batch-{batch_id:06d}.json"


def _audit_committed_batches(
    output_dir: Path, checkpoint: dict[str, Any], identity: dict[str, str],
) -> None:
    """Fail closed if ANY historical receipt or aggregate is altered.

    Receipt hashes form a backwards-linked chain. Reading just the latest
    hash cannot establish that earlier frame evidence remains intact.
    """
    run_id = checkpoint["run_id"]
    expected_sha = checkpoint["last_batch_receipt_sha256"]
    expected_cursor = checkpoint["next_cursor"]
    cumulative = {
        "sample_count": 0,
        "dynamic_examined_window_count": 0,
        "dynamic_event_examined_window_count": 0,
        "effect_candidates_truncated_windows": 0,
        "violation_count": 0,
        "no_visible_ink_count": 0,
    }
    newest_samples = None
    for batch_id in range(checkpoint["batch_count"], 0, -1):
        path = _receipt_path(output_dir, run_id, batch_id)
        if path.is_symlink() or not path.is_file():
            raise ValueError("Committed ASS batch chain has a missing receipt")
        # The old verifier first re-opened the file to hash it and then
        # re-opened it again to parse JSON. That doubled the I/O and allowed
        # a between-read swap to pass the checksum while parsing new bytes.
        if path.stat().st_size > MAX_BATCH_RECEIPT_BYTES:
            raise ValueError("ASS batch receipt exceeds safe size bound")
        raw = path.read_bytes()
        if (len(raw) > MAX_BATCH_RECEIPT_BYTES or
                hashlib.sha256(raw).hexdigest() != expected_sha):
            raise ValueError("Committed ASS batch chain has changed bytes")
        item = json.loads(raw)
        if (not isinstance(item, dict)
                or item.get("schema_version") != VERSION
                or item.get("sampling_algorithm") != ALGORITHM
                or item.get("scope") != "bounded_sequential_window_batch"
                or item.get("full_interval_verified") is not False
                or item.get("run_id") != run_id
                or item.get("batch_id") != batch_id
                or item.get("next_cursor") != expected_cursor
                or any(item.get(k) != v for k, v in identity.items())
                or item.get("safe_area") != checkpoint.get("safe_area")):
            raise ValueError("Committed ASS batch provenance mismatch")
        cursor = item.get("cursor")
        count = item.get("window_count")
        samples = item.get("samples")
        if (type(cursor) is not int or type(count) is not int
                or cursor < 0 or count < 1 or cursor + count != expected_cursor
                or not isinstance(samples, list) or not 1 <= len(samples) <= 12):
            raise ValueError("Committed ASS batch cursor or sample count invalid")
        for key in ("dynamic_examined_window_count",
                    "dynamic_event_examined_window_count",
                    "effect_candidates_truncated_windows",
                    "violation_count", "no_visible_ink_count"):
            if type(item.get(key)) is not int or item[key] < 0:
                raise ValueError("Committed ASS batch has invalid counters")
        if (item["dynamic_event_examined_window_count"] >
                item["dynamic_examined_window_count"] or
                item["dynamic_examined_window_count"] > count):
            raise ValueError("Committed ASS batch dynamic counts are inconsistent")
        if any(
            not isinstance(row, dict)
            or type(row.get("window_index")) is not int
            or not cursor <= row["window_index"] < expected_cursor
            or row.get("status") not in {
                "inside_safe_area", "outside_safe_area", "no_visible_ink"
            }
            for row in samples
        ):
            raise ValueError("Committed ASS sample identity or status invalid")
        bad = sum(row["status"] == "outside_safe_area" for row in samples)
        missing = sum(row["status"] == "no_visible_ink" for row in samples)
        if item["violation_count"] != bad or item["no_visible_ink_count"] != missing:
            raise ValueError("Committed ASS batch sample totals do not match receipts")
        if newest_samples is None:
            newest_samples = samples
        cumulative["sample_count"] += len(samples)
        for key in cumulative:
            if key != "sample_count":
                cumulative[key] += item[key]
        prev = item.get("previous_batch_receipt_sha256")
        if not isinstance(prev, str):
            raise ValueError("Committed ASS receipt chain link invalid")
        expected_sha = prev
        expected_cursor = cursor
    if expected_cursor != 0 or expected_sha != "":
        raise ValueError("Committed ASS receipt chain does not reach genesis")
    if any(checkpoint.get(k) != value for k, value in cumulative.items()):
        raise ValueError("Committed ASS checkpoint counters disagree with historical receipts")
    if checkpoint.get("last_batch_samples") != newest_samples:
        raise ValueError("Committed ASS last batch samples differ from checkpoint")
    if (checkpoint.get("unexamined_unique_windows") !=
            checkpoint["unique_window_count"] - checkpoint["next_cursor"]
            or checkpoint.get("pass_complete") is not (
                checkpoint["next_cursor"] == checkpoint["unique_window_count"]
            )):
        raise ValueError("Committed ASS checkpoint claims invalid coverage")


def read_exported_ass_coverage(
    output_dir: Path | None, config: ProjectConfig | None = None,
) -> dict[str, Any]:
    if output_dir is None or config is None:
        return {"state": "not-built", "verified": False, "next_cursor": 0}
    checkpoint = output_dir / COVERAGE_FILE
    if not checkpoint.exists():
        return {"state": "unverified", "verified": False, "next_cursor": 0}
    try:
        if checkpoint.is_symlink():
            raise ValueError("Coverage checkpoint symlink")
        if checkpoint.stat().st_size > MAX_CHECKPOINT_BYTES:
            raise ValueError("Coverage checkpoint exceeds safe size bound")
        checkpoint_bytes = checkpoint.read_bytes()
        if len(checkpoint_bytes) > MAX_CHECKPOINT_BYTES:
            raise ValueError("Coverage checkpoint exceeds safe size bound")
        checkpoint_sha256 = hashlib.sha256(checkpoint_bytes).hexdigest()
        record = json.loads(checkpoint_bytes)
        if (not isinstance(record, dict) or record.get("schema_version") != VERSION
                or record.get("sampling_algorithm") != ALGORITHM
                or record.get("scope") != "bounded_sequential_window_pass"
                or record.get("full_interval_verified") is not False):
            raise ValueError("Unknown coverage checkpoint schema")
        for field in ("batch_count", "next_cursor", "unique_window_count",
                      "sample_count", "dynamic_window_count",
                      "dynamic_examined_window_count", "dynamic_event_examined_window_count",
                      "violation_count", "no_visible_ink_count",
                      "effect_candidates_truncated_windows", "unexamined_unique_windows"):
            if type(record.get(field)) is not int or record[field] < 0:
                raise ValueError("Invalid coverage checkpoint counters")
        if (not 0 < record["batch_count"] <= 200000
                or not 0 < record["next_cursor"] <= record["unique_window_count"] <= 200000
                or record["dynamic_examined_window_count"] > record["next_cursor"]
                or record["dynamic_event_examined_window_count"] >
                    record["dynamic_examined_window_count"]
                or record["violation_count"] + record["no_visible_ink_count"] >
                    record["sample_count"]):
            raise ValueError("Inconsistent coverage counters")
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("Invalid coverage run id")
        identity = _export_identity(output_dir, config)
        if any(record.get(k) != v for k, v in identity.items()):
            return {"state": "stale", "verified": False, "next_cursor": 0,
                    "reason": "Exported ASS, export receipt or style policy changed"}
        _audit_committed_batches(output_dir, record, identity)
        # Do not return verified=true for a snapshot that has already changed
        # while its potentially long historical receipt-chain audit ran.
        # This also catches a concurrent writer advancing the checkpoint.
        try:
            current_identity = _export_identity(output_dir, config)
        except (OSError, ValueError):
            # It can become unavailable because ASS/export receipt/style was
            # replaced in the middle of the scan. This is stale evidence,
            # not a malformed historical receipt.
            current_identity = None
        if (checkpoint.is_symlink() or _sha256(checkpoint) != checkpoint_sha256
                or current_identity != identity):
            return {
                "state": "stale", "verified": False, "next_cursor": 0,
                "reason": "ASS export identity or coverage checkpoint changed during audit",
            }
        return {
            "state": ("window_pass_complete" if record["next_cursor"] ==
                      record["unique_window_count"] else "partial"),
            "verified": True, "next_cursor": record["next_cursor"],
            "coverage": record,
        }
    except (OSError, ValueError, UnicodeError, TypeError, KeyError) as exc:
        return {"state": "stale", "verified": False, "next_cursor": 0,
                "reason": "Unreadable or incompatible coverage: " + type(exc).__name__}


def inspect_next_exported_ass_batch(
    config: ProjectConfig, output_dir: Path, *,
    sample_limit: int = 8, expected_cursor: int = 0, restart: bool = False,
    cancel_event: threading.Event | None = None,
    report_progress: Callable[[str, str, int, int], None] | None = None,
) -> dict[str, Any]:
    if type(sample_limit) is not int or not 2 <= sample_limit <= 12:
        raise ValueError("Coverage frame limit must be an integer from 2 to 12")
    if type(expected_cursor) is not int or expected_cursor < 0:
        raise ValueError("expected_cursor must be a nonnegative integer")
    if type(restart) is not bool:
        raise ValueError("restart must be a boolean")
    cancel_event = cancel_event if cancel_event is not None else threading.Event()
    started = time.monotonic()
    deadline = started + 120.0
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Sequential coverage requires FFmpeg/libass")
    identity = _export_identity(output_dir, config)
    existing = read_exported_ass_coverage(output_dir, config)
    if restart:
        if expected_cursor != 0:
            raise ValueError("Explicit restart requires expected_cursor=0")
        previous: dict[str, Any] | None = None
    elif existing["state"] == "unverified":
        previous = None
    elif existing["state"] == "partial":
        previous = existing["coverage"]
    elif existing["state"] == "window_pass_complete":
        raise ValueError("Window pass finished; explicitly restart to sample again")
    else:
        raise ValueError("Coverage is stale: explicitly restart after regenerating ASS")
    cursor = previous["next_cursor"] if previous else 0
    if expected_cursor != cursor:
        raise ValueError("Coverage cursor changed; reload the progress before starting")
    batch_id = previous["batch_count"] + 1 if previous else 1
    run_id = previous["run_id"] if previous else uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="hsr-ass-coverage-") as td:
        snapshot = Path(td) / "export.ass"
        shutil.copyfile(output_dir / ASS_NAME, snapshot)
        if _sha256(snapshot) != identity["ass_sha256"]:
            raise RuntimeError("ASS changed while creating the verification snapshot")
        plan = plan_export_ass_window_batch(
            snapshot.read_text(encoding="utf-8-sig"), cursor=cursor,
            sample_limit=sample_limit, cancel_event=cancel_event, deadline=deadline,
        )
        if previous and (
            plan["unique_window_count"] != previous["unique_window_count"]
            or plan["dynamic_window_count"] != previous["dynamic_window_count"]
        ):
            raise RuntimeError("Subtitle window totals changed after earlier batches")
        h, v = config.subtitle_margin_horizontal_percent, config.subtitle_margin_vertical_percent
        area = SafeArea(
            margin_left_percent=h, margin_right_percent=h,
            margin_top_percent=v, margin_bottom_percent=v,
        )
        safe = {"x_min": area.x_min, "x_max": area.x_max,
                "y_min": area.y_min, "y_max": area.y_max}
        samples: list[dict[str, Any]] = []
        for position, item in enumerate(plan["samples"]):
            if cancel_event.is_set():
                raise InterruptedError("ASS batch canceled; cursor unchanged")
            if time.monotonic() >= deadline:
                raise TimeoutError("ASS coverage batch reached global deadline")
            if report_progress:
                report_progress("libass-coverage",
                                f"窗口 {plan['cursor']}–{plan['next_cursor']}，帧 {position+1}/{plan['sample_count']}",
                                position, plan["sample_count"])
            stamp = item["timestamp_seconds"]
            rgb_black = _render_raw_rgb(
                ffmpeg, snapshot, timestamp=stamp, background="black",
                cancel_event=cancel_event,
                timeout_seconds=max(.25, min(12., deadline-time.monotonic())),
            )
            if cancel_event.is_set():
                raise InterruptedError("ASS batch canceled; cursor unchanged")
            rgb_white = _render_raw_rgb(
                ffmpeg, snapshot, timestamp=stamp, background="white",
                cancel_event=cancel_event,
                timeout_seconds=max(.25, min(12., deadline-time.monotonic())),
            )
            bbox = visible_ink_bbox(rgb_black, rgb_white, 1920, 1080, threshold=16)
            status = ("no_visible_ink" if bbox is None else
                      "inside_safe_area" if (
                          safe["x_min"] <= bbox[0] and bbox[2] <= safe["x_max"]
                          and safe["y_min"] <= bbox[1] and bbox[3] <= safe["y_max"]
                      ) else "outside_safe_area")
            samples.append({**item, "bbox": list(bbox) if bbox else None,
                            "status": status})
    if cancel_event.is_set():
        raise InterruptedError("ASS batch canceled; cursor unchanged")
    latest = load_project(Path(config.root))
    latest_output = resolve_project_path(latest, latest.output_dir)
    if (latest_output is None or latest_output.resolve() != output_dir.resolve()
            or _export_identity(output_dir, latest) != identity):
        raise RuntimeError("ASS, receipt or saved policy changed during batch verification")
    current = read_exported_ass_coverage(output_dir, latest)
    if not restart and (
        (previous is None and current["state"] != "unverified") or
        (previous is not None and (
            current["state"] != "partial" or
            current["coverage"]["last_batch_receipt_sha256"] !=
                previous["last_batch_receipt_sha256"]
        ))
    ):
        raise RuntimeError("Another process advanced the coverage ledger")
    violations = sum(s["status"] == "outside_safe_area" for s in samples)
    invisible = sum(s["status"] == "no_visible_ink" for s in samples)
    receipt = {
        "schema_version": VERSION, "sampling_algorithm": ALGORITHM,
        "scope": "bounded_sequential_window_batch", "full_interval_verified": False,
        "batch_id": batch_id, "run_id": run_id,
        "previous_batch_receipt_sha256": (
            previous["last_batch_receipt_sha256"] if previous else ""
        ),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        **identity, "safe_area": safe,
        "cursor": cursor, "next_cursor": plan["next_cursor"],
        "window_count": plan["window_count"],
        "dynamic_examined_window_count": plan["dynamic_examined_window_count"],
        "dynamic_event_examined_window_count": plan["dynamic_event_examined_window_count"],
        "effect_candidates_truncated_windows": plan["effect_candidates_truncated_windows"],
        "violation_count": violations, "no_visible_ink_count": invisible,
        "samples": samples,
    }
    checkpoint = {
        "schema_version": VERSION, "sampling_algorithm": ALGORITHM,
        "scope": "bounded_sequential_window_pass", "full_interval_verified": False,
        "captured_at": receipt["captured_at"], "run_id": run_id,
        **identity, "safe_area": safe,
        "dialogue_count": plan["dialogue_count"],
        "unique_window_count": plan["unique_window_count"],
        "dynamic_window_count": plan["dynamic_window_count"],
        "next_cursor": plan["next_cursor"],
        "unexamined_unique_windows": plan["unique_window_count"] - plan["next_cursor"],
        "batch_count": batch_id,
        "sample_count": (previous["sample_count"] if previous else 0) + len(samples),
        "dynamic_examined_window_count": (
            (previous["dynamic_examined_window_count"] if previous else 0)
            + plan["dynamic_examined_window_count"]
        ),
        "dynamic_event_examined_window_count": (
            (previous["dynamic_event_examined_window_count"] if previous else 0)
            + plan["dynamic_event_examined_window_count"]
        ),
        "effect_candidates_truncated_windows": (
            (previous["effect_candidates_truncated_windows"] if previous else 0)
            + plan["effect_candidates_truncated_windows"]
        ),
        "violation_count": (previous["violation_count"] if previous else 0) + violations,
        "no_visible_ink_count": (previous["no_visible_ink_count"] if previous else 0) + invisible,
        "elapsed_seconds_total": round(
            (previous.get("elapsed_seconds_total", 0) if previous else 0)
            + time.monotonic() - started, 3
        ),
        "last_batch_samples": samples,
        "last_batch_receipt_sha256": "",
        "pass_complete": plan["pass_complete"],
    }
    if cancel_event.is_set():
        raise InterruptedError("ASS batch canceled before persistence")
    target = _receipt_path(output_dir, run_id, batch_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink() or target.is_symlink():
        raise ValueError("Coverage receipt path is a symlink")
    atomic_write_text(target, json.dumps(receipt, indent=2, ensure_ascii=False))
    checkpoint["last_batch_receipt_sha256"] = _sha256(target)
    if cancel_event.is_set():
        raise InterruptedError("ASS batch canceled before checkpoint commit")
    atomic_write_text(
        output_dir / COVERAGE_FILE, json.dumps(checkpoint, indent=2, ensure_ascii=False)
    )
    if report_progress:
        report_progress("complete", "本批检查点已保存；中间连续时间仍未验证",
                        len(samples), len(samples))
    return checkpoint
