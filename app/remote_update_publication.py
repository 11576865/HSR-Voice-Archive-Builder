"""Recoverable publication boundary for remote HSR voice additions.

All audio and index versions are created before this module is called.
The only active-state switch is the atomic on-disk ProjectConfig save.
A durable prepare receipt makes crashes before/after the switch inspectable.
This module does not claim cross-process locking or fully atomic multi-file IO.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .builder import atomic_write_text
from .project import ProjectConfig, project_file, resolve_project_path, update_project

RECEIPTS = "remote_update_transactions"
VERSION_RE = re.compile(r"^[0-9a-f]{32}$")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _store(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def _receipt_path(config: ProjectConfig, version: str) -> Path:
    if not VERSION_RE.fullmatch(version):
        raise ValueError("Invalid remote update publication version")
    return Path(config.root).resolve() / ".generated" / RECEIPTS / f"{version}.json"


def _resolved(root: Path, value: str) -> Path | None:
    value = str(value or "").strip()
    return (root / value).resolve() if value else None


def _matches(current: dict[str, Any], expected: dict[str, Any], root: Path) -> bool:
    return all(
        _resolved(root, current.get(field, "")) == _resolved(root, expected.get(field, ""))
        for field in ("wav_source", "index_csv")
    )


def _fresh_state(
    config: ProjectConfig,
    *,
    expected_config_sha256: str,
    expected_manifest_sha256: str,
    expected_index_sha256: str,
    manifest_path: Path,
    index_path: Path,
) -> tuple[Path, dict[str, Any]]:
    path = project_file(Path(config.root))
    if not path.is_file() or file_sha256(path) != expected_config_sha256:
        raise ValueError("Project configuration changed during remote update; check again")
    if not manifest_path.is_file() or file_sha256(manifest_path) != expected_manifest_sha256:
        raise ValueError("Manifest changed during remote update; check again")
    if not index_path.is_file() or file_sha256(index_path) != expected_index_sha256:
        raise ValueError("Index changed during remote update; check again")
    previous = json.loads(path.read_text(encoding="utf-8"))
    if not _matches(previous, {
        "wav_source": config.wav_source, "index_csv": config.index_csv
    }, Path(config.root).resolve()):
        raise ValueError("Running project configuration differs from persisted source")
    return path, previous


def commit_remote_update(
    config: ProjectConfig,
    *,
    version: str,
    combined: Path,
    updated_index: Path,
    manifest_path: Path,
    current_index: Path,
    expected_config_sha256: str,
    expected_manifest_sha256: str,
    expected_index_sha256: str,
    report_path: Path,
    report: dict[str, Any],
) -> dict[str, Any]:
    """Switch the project only after fully staged resources exist.

    A crash after the config switch but before receipt completion is handled
    by inspect_remote_updates: 'active_needs_reconciliation' is distinguishable
    from 'not_published'. Never delete prior audio/index automatically.
    """
    root = Path(config.root).resolve()
    generated = root / ".generated"
    if combined.resolve() != generated / "combined_audio_versions" / f"combined-{version}":
        raise ValueError("Invalid staged combined source path")
    if updated_index.resolve() != generated / f"quick_index_incremental_{version}.csv":
        raise ValueError("Invalid staged incremental index path")
    if not combined.is_dir() or not any(combined.rglob("*.wav")):
        raise ValueError("Combined audio version missing or has no WAV files")
    if not updated_index.is_file() or not updated_index.stat().st_size:
        raise ValueError("Incremental CSV version is missing or empty")
    project_path, previous = _fresh_state(
        config,
        expected_config_sha256=expected_config_sha256,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_index_sha256=expected_index_sha256,
        manifest_path=manifest_path,
        index_path=current_index,
    )
    next_paths = {"wav_source": str(combined), "index_csv": str(updated_index)}
    previous_paths = {
        "wav_source": previous.get("wav_source", ""),
        "index_csv": previous.get("index_csv", ""),
        "wav_source_fingerprint": previous.get("wav_source_fingerprint", ""),
        "reference_text_embedded": bool(previous.get("reference_text_embedded", False)),
    }
    receipt_path = _receipt_path(config, version)
    if receipt_path.exists():
        raise ValueError("Duplicate remote publication transaction")
    receipt = {
        "schema_version": 1,
        "version": version,
        "state": "prepared",
        "previous": previous_paths,
        "next": next_paths,
        "expected_project_sha256": expected_config_sha256,
        "expected_manifest_sha256": expected_manifest_sha256,
        "expected_index_sha256": expected_index_sha256,
        "new_index_sha256": file_sha256(updated_index),
        "applied_filenames": list(report.get("applied_filenames") or []),
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    _store(receipt_path, receipt)

    try:
        update_project(
            config,
            wav_source=str(combined),
            index_csv=str(updated_index),
            wav_source_fingerprint="",
        )
    except Exception:
        # A failed save may have left the Python dataclass modified. Trust
        # what was actually persisted, never the in-memory state alone.
        on_disk = json.loads(project_path.read_text(encoding="utf-8"))
        if _matches(on_disk, previous_paths, root):
            config.wav_source = str(previous_paths["wav_source"])
            config.index_csv = str(previous_paths["index_csv"])
            config.wav_source_fingerprint = str(previous_paths["wav_source_fingerprint"])
            config.reference_text_embedded = bool(previous_paths["reference_text_embedded"])
            receipt["state"] = "not_published"
            _store(receipt_path, receipt)
        # If disk points to the new revision, the receipt stays 'prepared';
        # inspection can reconcile it after the process restarts.
        raise

    # No exceptions after the successful config switch: return warnings
    # rather than reporting a failed Apply when the new project is active.
    warnings: list[str] = []
    receipt["state"] = "committed"
    receipt["committed_project_sha256"] = file_sha256(project_path)
    try:
        _store(receipt_path, receipt)
    except (OSError, ValueError) as exc:
        warnings.append(f"Publication receipt requires reconciliation: {exc}")
    try:
        _store(report_path, report)
    except (OSError, ValueError) as exc:
        warnings.append(f"Legacy Apply report not updated: {exc}")
    return {"version": version, "receipt": str(receipt_path), "warnings": warnings}


def inspect_remote_updates(config: ProjectConfig) -> dict[str, Any]:
    root = Path(config.root).resolve()
    current_path = project_file(root)
    if not current_path.is_file():
        return {"transactions": [], "project_sha256": ""}
    current_bytes = current_path.read_bytes()
    current = json.loads(current_bytes.decode("utf-8"))
    directory = root / ".generated" / RECEIPTS
    transactions: list[dict[str, Any]] = []
    for file in sorted(directory.glob("*.json"), reverse=True)[:60] if directory.is_dir() else []:
        try:
            entry = json.loads(file.read_text(encoding="utf-8"))
            if entry.get("schema_version") != 1 or entry.get("version") != file.stem:
                continue
            previous, next_paths = entry["previous"], entry["next"]
            if _matches(current, next_paths, root):
                state = ("active" if entry.get("state") == "committed"
                         else "active_needs_reconciliation")
            elif _matches(current, previous, root):
                state = "not_published" if entry.get("state") != "rolled_back" else "rolled_back"
            else:
                state = "superseded"
            transactions.append({
                "version": entry["version"],
                "status": state,
                "applied_filenames": entry.get("applied_filenames", []),
                "source_exists": bool((_resolved(root, next_paths["wav_source"]) or Path("/nonexistent")).is_dir()),
                "index_exists": bool((_resolved(root, next_paths["index_csv"]) or Path("/nonexistent")).is_file()),
            })
        except (OSError, KeyError, ValueError, TypeError):
            transactions.append({"version": file.stem, "status": "corrupt_receipt"})
    return {
        "project_sha256": hashlib.sha256(current_bytes).hexdigest(),
        "transactions": transactions,
    }


def rollback_remote_update(
    config: ProjectConfig,
    version: str,
    *,
    expected_project_sha256: str,
    manifest_path: Path,
) -> dict[str, Any]:
    """Explicitly roll back a not-yet-rebuilt, unchanged active revision."""
    receipt_path = _receipt_path(config, version)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("schema_version") != 1 or receipt.get("version") != version:
        raise ValueError("Invalid remote update receipt")
    project_path = project_file(Path(config.root))
    if not expected_project_sha256 or file_sha256(project_path) != expected_project_sha256:
        raise ValueError("Project changed since rollback review; inspect again")
    if (not manifest_path.is_file()
            or file_sha256(manifest_path) != receipt.get("expected_manifest_sha256")):
        raise ValueError("Archive was rebuilt after Apply; cannot automatically roll back")
    current = json.loads(project_path.read_text(encoding="utf-8"))
    root = Path(config.root).resolve()
    if not _matches(current, receipt["next"], root):
        raise ValueError("Selected remote update is not the active project revision")
    old = receipt["previous"]
    source = _resolved(root, old.get("wav_source", ""))
    index = _resolved(root, old.get("index_csv", ""))
    if source is None or not source.exists() or index is None or not index.is_file():
        raise ValueError("Previous audio or index is missing; cannot roll back")
    # Keep the newer resources and receipt for later inspection. Never delete
    # restored/cached original WAVs just because a rollback is requested.
    update_project(
        config,
        wav_source=old["wav_source"],
        index_csv=old["index_csv"],
        wav_source_fingerprint=old["wav_source_fingerprint"],
        reference_text_embedded=old["reference_text_embedded"],
    )
    receipt["state"] = "rolled_back"
    receipt["rolled_back_project_sha256"] = file_sha256(project_path)
    warnings: list[str] = []
    try:
        _store(receipt_path, receipt)
    except (OSError, ValueError) as exc:
        warnings.append(f"Rollback receipt needs reconciliation: {exc}")
    return {"rolled_back": True, "version": version, "warnings": warnings}
