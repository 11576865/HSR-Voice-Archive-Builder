"""Explicit, identity-checked recovery of human Chinese subtitle corrections.

Preview is read-only. Apply only restores user-selected active corrections,
never original-language scripts, review confirmations, timing or revision
history as if they belonged to the current project.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .builder import atomic_write_text
from .project import ProjectConfig, resolve_project_path
from .recovery import _validate_recovery_package_bytes
from .subtitle_timing import subtitle_edit_lock
from .subtitles import (
    _entry_id,
    _official_source_sha256,
    _update_project_subtitles_locked,
    load_subtitle_overrides,
)


def _recovery_snapshot(
    config: ProjectConfig,
    recovery_text: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = str(recovery_text or "").encode("utf-8")
    _, files = _validate_recovery_package_bytes(raw, require_checkpoint=False)
    backup_digest = hashlib.sha256(raw).hexdigest()
    saved_bytes = files.get("output/subtitles_overrides.json")
    if saved_bytes is None:
        raise ValueError("Recovery package has no active human Chinese subtitle corrections")
    try:
        saved = json.loads(saved_bytes.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Invalid human subtitles JSON in recovery package") from exc
    if not isinstance(saved, dict):
        raise ValueError("Recovery human subtitles must be a mapping")

    output = resolve_project_path(config, config.output_dir)
    manifest_path = output / "manifest.json" if output is not None else None
    if manifest_path is None or not manifest_path.is_file():
        raise ValueError("Build or open a real project Manifest before reviewing restoration")
    manifest_bytes = manifest_path.read_bytes()
    document = json.loads(manifest_bytes.decode("utf-8"))
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Invalid current project Manifest")

    members: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if isinstance(entry, dict):
            member = str(entry.get("source_member_id") or "").replace("\\", "/")
            if member:
                members.setdefault(member, []).append(entry)
    recorded_members = Counter(
        str(value.get("source_member_id") or "").replace("\\", "/")
        for value in saved.values()
        if isinstance(value, dict) and value.get("source_member_id")
    )
    existing = load_subtitle_overrides(output)
    existing_members = {
        str(record.get("source_member_id") or "").replace("\\", "/")
        for record in existing.values() if isinstance(record, dict)
    }
    existing_keys = set(existing)
    current_ids = Counter(
        str(_entry_id(row)) for row in entries if isinstance(row, dict)
    )

    result_rows: list[dict[str, Any]] = []
    for old_key, record in saved.items():
        record = record if isinstance(record, dict) else {}
        member = str(record.get("source_member_id") or "").replace("\\", "/")
        text = record.get("final_chs")
        match = members.get(member, []) if member else []
        status = "available"
        if not member or len(match) != 1 or recorded_members.get(member, 0) != 1:
            status = "source_missing_or_ambiguous"
        elif current_ids.get(str(_entry_id(match[0])), 0) != 1:
            status = "source_missing_or_ambiguous"
        elif not isinstance(text, str) or not text.strip():
            status = "empty_translation"
        elif record.get("source_text_hash_version") != 2:
            # Older sha scheme deleted *all* word boundaries, so matching it
            # cannot prove that the human edit belongs to this sentence.
            status = "legacy_provenance"
        elif (str(record.get("source_text_sha256") or "").lower()
              != _official_source_sha256(match[0])):
            status = "source_text_changed"
        else:
            audio_before = str(record.get("source_audio_sha256") or "").lower().strip()
            audio_now = str(match[0].get("sha256") or "").lower().strip()
            if audio_before and audio_now and audio_before != audio_now:
                status = "source_audio_changed"
            elif (audio_now and not audio_before) or (audio_before and not audio_now):
                status = "missing_audio_provenance"
            elif (member in existing_members
                  or str(_entry_id(match[0])) in existing_keys):
                # Never overwrite present proofreading, including a stale
                # current record whose old source evidence needs manual review.
                status = "current_edit_exists"
            elif text == str(match[0].get("target_text") or match[0].get("chinese") or ""):
                status = "already_base"
        entry = match[0] if len(match) == 1 else {}
        result_rows.append({
            "backup_key": str(old_key),
            "source_member_id": member,
            "current_id": _entry_id(entry) if entry else None,
            "status": status,
            "restorable": status == "available",
            "saved_chinese": text if isinstance(text, str) else "",
            "current_chinese": str(entry.get("target_text") or entry.get("chinese") or ""),
        })

    info = {
        "backup_sha256": backup_digest,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "project_root": str(Path(config.root).expanduser().resolve()),
        "rows": result_rows,
        "counts": dict(Counter(row["status"] for row in result_rows)),
        "restorable": sum(1 for row in result_rows if row["restorable"]),
        "total": len(result_rows),
    }
    return info, {"output": output, "raw": raw}


def preview_human_subtitle_recovery(
    config: ProjectConfig, recovery_text: str,
) -> dict[str, Any]:
    preview, _ = _recovery_snapshot(config, recovery_text)
    return preview


def apply_human_subtitle_recovery(
    config: ProjectConfig,
    recovery_text: str,
    *,
    selected_source_members: list[str],
    expected_backup_sha256: str,
    expected_manifest_sha256: str,
    expected_project_root: str,
) -> dict[str, Any]:
    if (not isinstance(selected_source_members, list)
            or not selected_source_members
            or any(not isinstance(item, str) or not item for item in selected_source_members)
            or len(selected_source_members) != len(set(selected_source_members))):
        raise ValueError("Select at least one distinct source member to restore")
    if len(selected_source_members) > 2000:
        raise ValueError("Too many subtitle revisions selected for one restoration")
    output = resolve_project_path(config, config.output_dir)
    if output is None:
        raise ValueError("Missing project output")
    # Serialized with proofreading saves, timing operations and export. The
    # manifest and backup identities are re-read *inside* the same lock.
    with subtitle_edit_lock(output):
        preview, context = _recovery_snapshot(config, recovery_text)
        if (not expected_project_root
                or expected_project_root != preview["project_root"]
                or not expected_backup_sha256
                or expected_backup_sha256 != preview["backup_sha256"]
                or not expected_manifest_sha256
                or expected_manifest_sha256 != preview["manifest_sha256"]):
            raise ValueError("Recovery project, backup or Manifest changed; preview again")

        indexed = {row["source_member_id"]: row for row in preview["rows"]
                   if row["source_member_id"]}
        updates = []
        for member in selected_source_members:
            row = indexed.get(member)
            if row is None or not row["restorable"]:
                raise ValueError(f"Source member is not safely restorable: {member}")
            current_id = row["current_id"]
            if current_id is None:
                raise ValueError("Missing current subtitle item ID")
            updates.append({
                "id": current_id,
                "final_chs": row["saved_chinese"],
                # Existing review receipts certify a different editorial
                # state; the restored translation must be proofread again.
                "confirmed": False,
                "expected_final_chs": row["current_chinese"],
                "expected_source_member_id": member,
                "expected_source_text_sha256": _current_source_digest(
                    context["output"], current_id, member
                ),
            })
        state = resolve_project_path(config, config.state_dir)
        if state is None:
            raise ValueError("No project state directory for verified recovery evidence")
        stash = state / "recovery_imports"
        stash.mkdir(parents=True, exist_ok=True)
        preserved = stash / f"subtitle-recovery-{preview['backup_sha256']}.hsrbackup"
        if not preserved.exists():
            atomic_write_text(preserved, context["raw"].decode("utf-8"))
        result = _update_project_subtitles_locked(config, output, updates)
        return {
            "restored_count": result["updated_count"],
            "restored_source_members": list(selected_source_members),
            "backup_sha256": preview["backup_sha256"],
            "preserved_package": str(preserved),
            "export_status": result.get("export_status"),
            "artifact_error": result.get("artifact_error") or "",
        }


def _current_source_digest(output: Path, item_id: Any, member: str) -> str:
    data = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    found = [
        item for item in data.get("entries", [])
        if isinstance(item, dict) and _entry_id(item) == item_id
        and str(item.get("source_member_id") or "").replace("\\", "/") == member
    ]
    if len(found) != 1:
        raise ValueError("Source member changed during subtitle recovery")
    return _official_source_sha256(found[0])
