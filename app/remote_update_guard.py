"""Conservative identity boundary for HSR incremental voice adoption.

Remote dataset resolution uses WAV *basenames*. Unlike local package scans,
it cannot presently prove the identity of parallel paths with the same
basename. Reject these collisions instead of overwriting original speech.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path, PurePosixPath
from typing import Any


def _simple_filename(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if (not text or len(path.parts) != 1 or path.name != text
            or text in {".", ".."} or not text.casefold().endswith(".wav")
            or ":" in text or "\x00" in text):
        raise ValueError(f"Cannot safely adopt a non-basename WAV identity: {text!r}")
    return text


def _existing_names(manifest_path: Path, index_path: Path | None) -> set[str]:
    names: set[str] = set()
    if manifest_path.is_file():
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        rows = payload.get("entries", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("Invalid existing voice manifest")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid voice manifest entry")
            for value in (row.get("filename"), row.get("source_member_id")):
                if value:
                    names.add(PurePosixPath(str(value).replace("\\", "/")).name.casefold())
    if index_path is not None and index_path.is_file():
        with index_path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                for value in (row.get("filename"), row.get("source_member_id"),
                              row.get("来源成员路径"), row.get("文件名")):
                    if value:
                        names.add(PurePosixPath(str(value).replace("\\", "/")).name.casefold())
    return names


def choose_remote_additions(
    remote_plan: dict[str, Any],
    manifest_path: Path,
    index_path: Path | None,
    *,
    selected_filenames: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Validate an explicit selection against a persisted remote-only plan.

    None means adopt *all* unambiguous new basenames (legacy API behavior).
    [] means an intentionally empty selection, and must not mean all.
    """
    if not isinstance(remote_plan, dict) or "remote_rows" not in remote_plan:
        raise ValueError("Please check the remote index before applying updates")
    if not manifest_path.is_file():
        raise FileNotFoundError("Existing archive manifest not found; build first")
    import hashlib
    current_manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if remote_plan.get("manifest_sha256") != current_manifest_hash:
        raise ValueError("Archive changed since update check; refresh remote comparison before applying")
    rows = remote_plan.get("new_logical")
    if not isinstance(rows, list):
        raise ValueError("Invalid remote update plan")
    allowed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("metadata"), dict):
            raise ValueError("Remote update plan contains invalid candidate")
        filename = _simple_filename(row.get("filename", ""))
        declared = _simple_filename(row["metadata"].get("filename", ""))
        if filename.casefold() != declared.casefold():
            raise ValueError(f"Remote index entry identity mismatch: {filename}")
        key = filename.casefold()
        if key in allowed:
            raise ValueError(f"Remote update plan contains duplicate basename: {filename}")
        allowed[key] = row["metadata"]
    if selected_filenames is None:
        selected_keys = set(allowed)
    else:
        if not isinstance(selected_filenames, list):
            raise ValueError("Selected voice filenames must be a list")
        selected_keys = set()
        for value in selected_filenames:
            key = _simple_filename(value).casefold()
            if key in selected_keys:
                raise ValueError(f"Duplicate selected voice filename: {value}")
            if key not in allowed:
                raise ValueError(f"Selection is not an unambiguous new voice: {value}")
            selected_keys.add(key)
    if not selected_keys:
        raise ValueError("Please select at least one new voice")

    existing = _existing_names(manifest_path, index_path)
    overlap = selected_keys & existing
    if overlap:
        raise ValueError(
            "New voice would collide with an existing archive basename; "
            "identity resolution required: " + ", ".join(sorted(overlap)[:4])
        )
    return [dict(metadata) for key, metadata in allowed.items() if key in selected_keys]


def verify_combined_source_no_collisions(extracted_root: Path, incoming_dir: Path) -> None:
    """Fail closed before copying any downloaded WAV into a combined archive."""
    existing: set[str] = set()
    for path in extracted_root.rglob("*"):
        if path.is_file() and path.suffix.casefold() == ".wav":
            existing.add(path.name.casefold())
    incoming: set[str] = set()
    for path in incoming_dir.iterdir():
        if not path.is_file() or path.name.endswith(".part"):
            continue
        name = _simple_filename(path.name)
        if path.is_symlink():
            raise ValueError(f"Unsafe downloaded voice symlink: {name}")
        key = name.casefold()
        if key in incoming:
            raise ValueError(f"Duplicate downloaded WAV identity: {name}")
        incoming.add(key)
        if key in existing:
            raise ValueError(
                f"Downloaded voice basename collides with original archive WAV: {name}"
            )
