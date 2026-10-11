"""Auditable two-phase publication and conservative rollback for HSR remote additions.

The journal is written *before* the atomic project config replacement.
A process can die between that replacement and the final journal update;
inspection therefore derives the live state from exact disk hashes, never
from the journal's optimistic 'committed' string alone.

This is intentionally NOT a cross-process lock or multi-file transaction.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .project import (
    PROJECT_FILENAME, ProjectConfig, _portable_path,
    atomic_write_json, resolve_project_path, save_project,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _revision_dir(config: ProjectConfig) -> Path:
    return Path(config.root).resolve() / ".generated" / "incremental_revisions"


def _revision_file(config: ProjectConfig, revision_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", str(revision_id)):
        raise ValueError("Invalid incremental revision identifier")
    return _revision_dir(config) / f"{revision_id}.json"


def capture_update_boundary(config: ProjectConfig, manifest_path: Path) -> dict[str, str]:
    root = Path(config.root).resolve()
    config_file = root / PROJECT_FILENAME
    index = resolve_project_path(config, config.index_csv)
    if index is None or not index.is_file() or not config_file.is_file():
        raise FileNotFoundError("Existing index or project configuration missing")
    if not manifest_path.is_file():
        raise FileNotFoundError("Existing archive Manifest missing")
    return {
        "project_sha256": _sha(config_file),
        "index_sha256": _sha(index),
        "manifest_sha256": _sha(manifest_path),
    }


def _assert_boundary(config: ProjectConfig, manifest_path: Path,
                     expected: dict[str, str]) -> None:
    current = capture_update_boundary(config, manifest_path)
    if current != expected:
        raise ValueError(
            "Project, index or Manifest changed during remote update; "
            "refresh comparison before publishing"
        )


def publish_remote_revision(
    config: ProjectConfig, *,
    manifest_path: Path,
    expected_boundary: dict[str, str],
    combined: Path,
    updated_index: Path,
    revision_id: str,
    selected_filenames: list[str],
) -> dict[str, Any]:
    """Publish completed immutable resources via one atomic config replacement."""
    root = Path(config.root).resolve()
    combined = combined.resolve()
    updated_index = updated_index.resolve()
    revision_path = _revision_file(config, revision_id)
    if (combined != root / ".generated" / "combined_audio_versions" / f"combined-{revision_id}"
            or updated_index != root / ".generated" / f"quick_index_incremental_{revision_id}.csv"):
        raise ValueError("Incremental published resource path does not match revision identity")
    if not combined.is_dir() or not list(combined.rglob("*.wav")):
        raise ValueError("Published audio version is empty or missing")
    if not updated_index.is_file() or not updated_index.stat().st_size:
        raise ValueError("Published index version is empty or missing")
    if not selected_filenames or len(set(selected_filenames)) != len(selected_filenames):
        raise ValueError("Invalid revision selection")
    selected = {name.casefold() for name in selected_filenames}
    if len(selected) != len(selected_filenames):
        raise ValueError("Selected filenames differ only by case")
    if any(Path(name).name != name or not name.lower().endswith(".wav")
           for name in selected_filenames):
        raise ValueError("Selected WAV identity must be a simple basename")
    # Validate the final published pair, not merely the incoming download
    # count. This catches a malformed generated index and missing staged WAV
    # before the project config is redirected to immutable resources.
    with updated_index.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or "filename" not in reader.fieldnames:
            raise ValueError("Published index is missing filename column")
        rows = list(reader)
    if not rows:
        raise ValueError("Published index contains no voice records")
    # The new index is a complete replacement, not a list of additions.
    # Validate every existing chapter-specific WAV member too; a partial
    # staged tree must never become the project's active source directory.
    counts: dict[str, int] = {}
    seen_members: set[str] = set()
    for number, row in enumerate(rows, 1):
        filename = str(row.get("filename") or "").strip().replace("\\", "/")
        member = str(row.get("source_member_id") or filename).strip().replace("\\", "/")
        if (not filename or not member or
                any(part in {"", ".", ".."} for part in member.split("/")) or
                member.startswith("/") or ":" in member or "\x00" in member or
                not member.casefold().endswith(".wav")):
            raise ValueError(f"Unsafe published source-member identity on row {number}")
        # Filename can be a package-relative name in historic indexes; its
        # basename still has to identify the same physical WAV as member.
        if (any(part in {"", ".", ".."} for part in filename.split("/"))
                or filename.startswith("/")
                or Path(filename).name.casefold() != member.split("/")[-1].casefold()
                or ":" in filename or "\x00" in filename):
            raise ValueError(f"Published WAV filename/member conflict on row {number}")
        key = member.casefold()
        if key in seen_members:
            raise ValueError(f"Duplicate published source-member identity: {member}")
        seen_members.add(key)
        if filename.casefold() in selected and member != filename:
            raise ValueError("Published added WAV source-member mismatch")
        current = combined
        for component in member.split("/"):
            current = current / component
            if current.is_symlink():
                raise ValueError(f"Unsafe published WAV symlink: {member}")
        if not current.is_file():
            raise ValueError(f"Published indexed WAV missing: {member}")
        if filename.casefold() in selected:
            counts[key] = counts.get(key, 0) + 1
    if set(counts) != selected or any(count != 1 for count in counts.values()):
        raise ValueError("Published index does not contain exactly the selected WAV records")
    if revision_path.exists():
        raise ValueError("Incremental revision ID already exists")

    _assert_boundary(config, manifest_path, expected_boundary)
    previous = asdict(config)
    proposed = copy.deepcopy(config)
    proposed.wav_source = _portable_path(root, combined)
    proposed.index_csv = _portable_path(root, updated_index)
    proposed.wav_source_fingerprint = ""
    if proposed.reference_text_embedded:
        proposed.reference_text_embedded = False

    # JSON bytes are predicted using precisely the format save_project uses.
    proposal_bytes = json.dumps(
        {**asdict(proposed), "root": str(root)},
        ensure_ascii=False, indent=2, default=str,
    ).encode("utf-8")
    expected_next_sha = hashlib.sha256(proposal_bytes).hexdigest()
    journal = {
        "schema_version": 1,
        "revision_id": revision_id,
        "created_at": _utc(),
        "state": "prepared",
        "previous_config": previous,
        "previous_boundary": dict(expected_boundary),
        "new_wav_source": str(combined),
        "new_index_csv": str(updated_index),
        "new_index_sha256": _sha(updated_index),
        "new_project_sha256": expected_next_sha,
        "selected_filenames": list(selected_filenames),
    }
    revision_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(revision_path, journal)

    # Re-check immediately before the config's atomic publication. The
    # preceding journal write must not entitle a stale proposal to commit.
    _assert_boundary(config, manifest_path, expected_boundary)
    if _sha(updated_index) != journal["new_index_sha256"]:
        raise ValueError("Published index changed after revision preparation")
    save_project(proposed)
    config.__dict__.update(vars(proposed))
    if _sha(root / PROJECT_FILENAME) != expected_next_sha:
        raise RuntimeError("Published project configuration digest differs from prepared revision")
    journal["state"] = "committed"
    journal["committed_at"] = _utc()
    atomic_write_json(revision_path, journal)
    return revision_status(config, revision_id)


def revision_status(config: ProjectConfig, revision_id: str) -> dict[str, Any]:
    journal_path = _revision_file(config, revision_id)
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("schema_version") != 1 or journal.get("revision_id") != revision_id:
        raise ValueError("Invalid incremental revision journal")
    project_file = Path(config.root).resolve() / PROJECT_FILENAME
    live_sha = _sha(project_file) if project_file.is_file() else ""
    next_sha = str(journal.get("new_project_sha256") or "")
    prior_sha = str((journal.get("previous_boundary") or {}).get("project_sha256") or "")
    if live_sha == next_sha:
        live_state = "active" if journal.get("state") == "committed" else "applied_unfinalized"
    elif live_sha == prior_sha:
        live_state = "rolled_back" if journal.get("state") == "rolled_back" else "not_applied"
    else:
        live_state = "superseded_or_modified"
    manifest = resolve_project_path(config, config.output_dir)
    manifest = manifest / "manifest.json" if manifest else None
    same_manifest = bool(
        manifest and manifest.is_file()
        and _sha(manifest) == (journal.get("previous_boundary") or {}).get("manifest_sha256")
    )
    # Existing active artifacts are never silently replaced by rollback.
    previous = journal.get("previous_config") or {}
    prev_source = resolve_project_path(config, str(previous.get("wav_source") or ""))
    prev_index = resolve_project_path(config, str(previous.get("index_csv") or ""))
    prior_resources_exist = bool(
        prev_source and prev_source.exists()
        and prev_index and prev_index.is_file()
    )
    # Existence alone cannot prove the old index still describes the old
    # source. Refuse rollback if either the previous or new index was edited
    # after publication, even if the project configuration still matches.
    previous_index_unchanged = bool(
        prev_index and prev_index.is_file()
        and _sha(prev_index) == str(
            (journal.get("previous_boundary") or {}).get("index_sha256") or ""
        )
    )
    next_index = Path(str(journal.get("new_index_csv") or ""))
    new_index_unchanged = bool(
        next_index.is_file()
        and _sha(next_index) == str(journal.get("new_index_sha256") or "")
    )
    return {
        "revision_id": revision_id,
        "state": live_state,
        "created_at": journal.get("created_at", ""),
        "selected_filenames": journal.get("selected_filenames", []),
        "current_project_sha256": live_sha,
        "expected_new_project_sha256": next_sha,
        "manifest_unchanged": same_manifest,
        "prior_resources_exist": prior_resources_exist,
        "prior_index_unchanged": previous_index_unchanged,
        "new_index_unchanged": new_index_unchanged,
        "can_rollback": live_state in {"active", "applied_unfinalized"}
            and same_manifest and prior_resources_exist
            and previous_index_unchanged and new_index_unchanged,
        "new_wav_source": str(journal.get("new_wav_source") or ""),
        "new_index_csv": str(journal.get("new_index_csv") or ""),
    }


def list_remote_revisions(config: ProjectConfig) -> list[dict[str, Any]]:
    directory = _revision_dir(config)
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.json"), reverse=True)[:40]
    records: list[dict[str, Any]] = []
    for file in files:
        try:
            records.append(revision_status(config, file.stem))
        except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError):
            records.append({"revision_id": file.stem, "state": "invalid_journal",
                            "can_rollback": False})
    return records


def rollback_remote_revision(
    config: ProjectConfig, revision_id: str, *,
    expected_project_sha256: str,
) -> dict[str, Any]:
    """Roll back only an untouched, directly active revision.

    A completed later build changes Manifest bytes and makes this unavailable;
    an index/config change also makes it unavailable. No audio is deleted.
    """
    journal_path = _revision_file(config, revision_id)
    status = revision_status(config, revision_id)
    if not status["can_rollback"]:
        raise ValueError("Revision is not the current unchanged project; rollback denied")
    if (not expected_project_sha256
            or expected_project_sha256 != status["current_project_sha256"]):
        raise ValueError("Project changed after rollback preview; refresh first")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    previous = journal.get("previous_config")
    if not isinstance(previous, dict) or previous.get("root") != str(Path(config.root).resolve()):
        raise ValueError("Unsafe rollback journal project identity")

    saved = ProjectConfig(**previous)
    root = Path(config.root).resolve()
    snapshot_bytes = json.dumps(
        {**asdict(saved), "root": str(root)},
        ensure_ascii=False, indent=2, default=str,
    ).encode("utf-8")
    if (hashlib.sha256(snapshot_bytes).hexdigest()
            != str((journal.get("previous_boundary") or {}).get("project_sha256") or "")):
        raise ValueError("Rollback journal previous configuration digest mismatch")
    journal["state"] = "rollback_pending"
    atomic_write_json(journal_path, journal)
    # The journal write is not a license to roll back if another actor has
    # changed the indexes or project while we were preparing.
    if not revision_status(config, revision_id)["can_rollback"]:
        raise ValueError("Rollback inputs changed during preparation")
    # Atomic replace; if a process exits between this and journal finalization,
    # inspection observes prior project digest and marks it not-applied.
    save_project(saved)
    config.__dict__.update(vars(saved))
    if _sha(Path(config.root) / PROJECT_FILENAME) != journal["previous_boundary"]["project_sha256"]:
        raise RuntimeError("Rollback restored config does not match original digest")
    journal["state"] = "rolled_back"
    journal["rolled_back_at"] = _utc()
    atomic_write_json(journal_path, journal)
    return revision_status(config, revision_id)
