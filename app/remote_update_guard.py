"""Conservative identity boundary for HSR incremental voice adoption.

Remote dataset resolution uses WAV *basenames*. Unlike local package scans,
it cannot presently prove the identity of parallel paths with the same
basename. Reject these collisions instead of overwriting original speech.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
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


def verified_completed_new_wavs(
    selected_targets: list[dict[str, Any]],
    downloaded: dict[str, Any],
    incoming_dir: Path,
) -> set[str]:
    """Accept only WAV bytes from this exact download session.

    The provider downloader can report an existing file without re-fetching it.
    Such a cache hit has no content hash and is never sufficient evidence of
    a newly resolved remote voice. Both server implementations must use a
    unique incoming directory *and* this fail-closed result validator.
    """
    if not isinstance(selected_targets, list) or not isinstance(downloaded, dict):
        raise ValueError("Invalid remote voice download result")
    expected: dict[str, str] = {}
    for target in selected_targets:
        if not isinstance(target, dict):
            raise ValueError("Invalid selected remote voice")
        name = _simple_filename(str(target.get("filename") or ""))
        if name.casefold() in expected:
            raise ValueError(f"Duplicate selected remote basename: {name}")
        expected[name.casefold()] = name
    completed = downloaded.get("completed")
    if not isinstance(completed, list):
        raise ValueError("Remote download result has no completed records")
    accepted: set[str] = set()
    for record in completed:
        if not isinstance(record, dict):
            raise ValueError("Invalid completed remote voice record")
        name = _simple_filename(str(record.get("filename") or ""))
        canonical = expected.get(name.casefold())
        if canonical != name:
            raise ValueError(f"Unexpected completed remote voice: {name}")
        if name in accepted:
            raise ValueError(f"Duplicate completed remote voice: {name}")
        if record.get("status") != "ok":
            raise ValueError(f"Unverified cached remote voice is not publishable: {name}")
        path = incoming_dir / name
        # Compare the direct (non-resolved) expected path; a symlink must
        # never turn a different external file into this download's evidence.
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Verified downloaded WAV missing: {name}")
        declared_path = str(record.get("path") or "")
        if not declared_path or Path(declared_path).absolute() != path.absolute():
            raise ValueError(f"Remote download path mismatch: {name}")
        expected_digest = str(record.get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise ValueError(f"Missing downloaded WAV checksum: {name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected_digest:
            raise ValueError(f"Downloaded WAV checksum mismatch: {name}")
        # A checksum only attests returned bytes, not the audio container.
        # Parse chunk boundaries without assuming PCM codec or decoding sound:
        # compressed/extended WAVE formats may be handled later by FFmpeg.
        # A forged 12-byte RIFF/WAVE prefix is not a playable WAV.
        total_bytes = path.stat().st_size
        with path.open("rb") as stream:
            header = stream.read(12)
            if (len(header) != 12 or header[:4] != b"RIFF"
                    or header[8:] != b"WAVE"):
                raise ValueError(f"Downloaded file is not a RIFF/WAVE audio file: {name}")
            declared_end = int.from_bytes(header[4:8], "little") + 8
            if declared_end > total_bytes or declared_end < 44:
                raise ValueError(f"Invalid downloaded WAVE container length: {name}")
            offset = 12
            has_format = False
            has_samples = False
            while offset + 8 <= declared_end:
                stream.seek(offset)
                chunk = stream.read(8)
                if len(chunk) != 8:
                    raise ValueError(f"Truncated downloaded WAVE chunk: {name}")
                size = int.from_bytes(chunk[4:8], "little")
                chunk_end = offset + 8 + size
                if chunk_end > declared_end:
                    raise ValueError(f"Invalid downloaded WAVE chunk size: {name}")
                if chunk[:4] == b"fmt " and size >= 16:
                    has_format = True
                elif chunk[:4] == b"data" and size > 0:
                    has_samples = True
                offset = chunk_end + (size & 1)
            if not has_format or not has_samples:
                raise ValueError(f"Downloaded WAVE lacks format or audio data: {name}")
        accepted.add(name)
    return accepted


def verify_combined_source_no_collisions(
    extracted_root: Path,
    incoming_dir: Path,
    *,
    successful_filenames: set[str] | None = None,
) -> None:
    """Fail closed before copying any downloaded WAV into a combined archive."""
    existing: set[str] = set()
    for path in extracted_root.rglob("*"):
        if path.is_file() and path.suffix.casefold() == ".wav":
            existing.add(path.name.casefold())
    selected = (
        {_simple_filename(name).casefold() for name in successful_filenames}
        if successful_filenames is not None else None
    )
    incoming: set[str] = set()
    for path in incoming_dir.iterdir():
        if not path.is_file() or path.name.endswith(".part"):
            continue
        if selected is not None and path.name.casefold() not in selected:
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
    if selected is not None and incoming != selected:
        missing = sorted(selected - incoming)
        raise ValueError("Downloaded voice files missing from staging: " + ", ".join(missing[:5]))
