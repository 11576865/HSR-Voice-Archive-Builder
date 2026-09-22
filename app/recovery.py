from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .builder import atomic_write_text
from .project import ProjectConfig, STATE_DIR, resolve_project_path
from .schema import normalize_index

RECOVERY_FORMAT = "hsr-voice-text-recovery"
RECOVERY_SCHEMA_VERSION = 1
MAX_RECOVERY_BYTES = 32 * 1024 * 1024
AUTO_RECOVERY_STATUS_FILE = "recovery_status.json"
AUTO_RECOVERY_LATEST_FILE = "latest.hsrbackup"

_STATE_FILES = (
    ".translation_checkpoint.json",
    "translation_qa.json",
    "semantic_qa.json",
    "translation_usage.json",
    ".official_review_checkpoint.json",
    "official_review.json",
    "official_review_usage.json",
)
_GENERATED_FILES = (
    "quick_index.csv",
    "quick_index_incremental.csv",
    "quick_scan.json",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", str(value or "").strip()).strip(".-")
    return cleaned[:80] or "HSR-Voice-Archive"


def _encode_file(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "size": len(data),
        "sha256": _sha256(data),
        "base64": base64.b64encode(data).decode("ascii"),
    }


def _decode_file(entry: object, logical_name: str) -> bytes:
    if not isinstance(entry, dict):
        raise ValueError(f"Invalid recovery entry: {logical_name}")
    try:
        data = base64.b64decode(str(entry.get("base64", "")), validate=True)
    except Exception as exc:
        raise ValueError(f"Invalid base64 in recovery entry: {logical_name}") from exc
    if len(data) != int(entry.get("size", -1)):
        raise ValueError(f"Recovery size mismatch: {logical_name}")
    if _sha256(data) != str(entry.get("sha256", "")):
        raise ValueError(f"Recovery checksum mismatch: {logical_name}")
    return data


def _translation_input_fingerprint(
    row: dict[str, str],
    source_language: str,
    target_language: str,
) -> str:
    payload = {
        "english": str(row.get("english", "")),
        "reference_text": str(row.get("reference_text", "")),
        "reference_language": str(row.get("reference_language", "")),
        "source_language": source_language,
        "target_language": target_language,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checkpoint_identity(payload: object) -> dict[str, str] | None:
    if not isinstance(payload, dict):
        return None
    schema = payload.get("schema_version")
    model = str(payload.get("model", ""))
    if schema == 1:
        return {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": model,
            "source_language": "en",
            "target_language": "zh-CN",
        }
    if schema == 2:
        return {
            "provider": str(payload.get("provider", "")),
            "base_url": str(payload.get("base_url", "")),
            "model": model,
            "source_language": "en",
            "target_language": "zh-CN",
        }
    if schema == 3:
        return {
            "provider": str(payload.get("provider", "")),
            "base_url": str(payload.get("base_url", "")),
            "model": model,
            "source_language": str(payload.get("source_language", "")),
            "target_language": str(payload.get("target_language", "")),
        }
    return None


def _read_json_bytes(data: bytes, logical_name: str) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid JSON recovery file: {logical_name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Recovery JSON must be an object: {logical_name}")
    return payload


def build_text_recovery(config: ProjectConfig) -> tuple[bytes, str, dict[str, Any]]:
    root = Path(config.root).expanduser().resolve()
    state = resolve_project_path(config, config.state_dir)
    index = resolve_project_path(config, config.index_csv)
    generated = root / ".generated"
    if state is None:
        raise ValueError("Project state directory is not configured")

    files: dict[str, dict[str, Any]] = {}
    for name in _STATE_FILES:
        path = state / name
        if path.is_file():
            files[f"state/{name}"] = _encode_file(path)

    for name in _GENERATED_FILES:
        path = generated / name
        if path.is_file():
            files[f"generated/{name}"] = _encode_file(path)

    if index is not None and index.is_file():
        files["indexes/current.csv"] = _encode_file(index)

    glossary = resolve_project_path(config, config.glossary_path)
    if glossary is not None and glossary.is_file():
        suffix = glossary.suffix.lower() if glossary.suffix else ".txt"
        files[f"glossary/current{suffix}"] = _encode_file(glossary)

    total_payload_bytes = sum(int(item["size"]) for item in files.values())
    if total_payload_bytes > MAX_RECOVERY_BYTES:
        raise ValueError(
            f"Text recovery data is unexpectedly large: {total_payload_bytes} bytes"
        )

    checkpoint_records = 0
    checkpoint = files.get("state/.translation_checkpoint.json")
    if checkpoint:
        try:
            payload = _read_json_bytes(
                _decode_file(checkpoint, "state/.translation_checkpoint.json"),
                "state/.translation_checkpoint.json",
            )
            records = payload.get("records", {})
            if isinstance(records, dict):
                checkpoint_records = len(records)
        except ValueError:
            checkpoint_records = 0

    now = datetime.now(timezone.utc)
    package = {
        "format": RECOVERY_FORMAT,
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "created_at": now.isoformat(),
        "project": {
            "name": config.name,
            "audio_language": config.audio_language,
            "source_text_language": config.source_text_language,
            "target_language": config.target_language,
            "reference_language": config.reference_language,
            "translation_model": config.translation_model,
        },
        "policy": {
            "contains_audio": False,
            "contains_final_media": False,
            "purpose": "Preserve API translation/checkpoint text and matching metadata.",
        },
        "files": files,
    }
    data = json.dumps(package, ensure_ascii=False, indent=2).encode("utf-8")
    if len(data) > MAX_RECOVERY_BYTES:
        raise ValueError(f"Recovery package exceeds {MAX_RECOVERY_BYTES} bytes")

    stamp = now.strftime("%Y%m%d-%H%M%SZ")
    filename = f"{_safe_name(config.name)}-Translation-Recovery-{stamp}.hsrbackup"
    summary = {
        "filename": filename,
        "package_bytes": len(data),
        "payload_bytes": total_payload_bytes,
        "included_files": sorted(files),
        "translation_records": checkpoint_records,
        "contains_audio": False,
    }
    return data, filename, summary


def _auto_recovery_dir(config: ProjectConfig) -> Path:
    root = str(Path(config.root).expanduser().resolve())
    project_id = hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
    return STATE_DIR / "recovery" / f"project-{project_id}"


def auto_recovery_status(config: ProjectConfig) -> dict[str, Any]:
    directory = _auto_recovery_dir(config)
    latest = directory / AUTO_RECOVERY_LATEST_FILE
    status_file = directory / AUTO_RECOVERY_STATUS_FILE
    status: dict[str, Any] = {}
    if status_file.is_file():
        try:
            loaded = json.loads(status_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                status = loaded
        except (OSError, ValueError, TypeError):
            status = {}
    has_backup = latest.is_file()
    return {
        "enabled": True,
        "has_backup": has_backup,
        "healthy": bool(status.get("healthy", has_backup)),
        "last_backup": str(status.get("last_backup", "")),
        "reason": str(status.get("reason", "")),
        "translation_records": int(status.get("translation_records", 0) or 0),
        "size_bytes": latest.stat().st_size if has_backup else 0,
        "path": str(latest),
        "directory": str(directory),
        "error": str(status.get("error", "")),
        "contains_audio": False,
    }


def write_auto_text_recovery(
    config: ProjectConfig,
    *,
    reason: str,
) -> dict[str, Any]:
    state = resolve_project_path(config, config.state_dir)
    checkpoint = state / ".translation_checkpoint.json" if state is not None else None
    if checkpoint is None or not checkpoint.is_file():
        status = auto_recovery_status(config)
        status.update({
            "reason": str(reason or "checkpoint-not-ready"),
            "skipped": True,
            "message": "尚无 API 翻译检查点，不需要创建自动恢复包。",
        })
        return status

    data, _filename, summary = build_text_recovery(config)
    directory = _auto_recovery_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    latest = directory / AUTO_RECOVERY_LATEST_FILE
    status_file = directory / AUTO_RECOVERY_STATUS_FILE
    atomic_write_text(latest, data.decode("utf-8"))
    now = datetime.now(timezone.utc).isoformat()
    status = {
        "schema_version": 1,
        "healthy": True,
        "last_backup": now,
        "reason": str(reason or "unspecified"),
        "translation_records": int(summary.get("translation_records", 0) or 0),
        "size_bytes": latest.stat().st_size,
        "path": str(latest),
        "project_name": config.name,
        "project_root": str(Path(config.root).expanduser().resolve()),
        "contains_audio": False,
        "error": "",
    }
    atomic_write_text(
        status_file,
        json.dumps(status, ensure_ascii=False, indent=2),
    )
    return auto_recovery_status(config)


def try_write_auto_text_recovery(
    config: ProjectConfig,
    *,
    reason: str,
) -> dict[str, Any]:
    try:
        return write_auto_text_recovery(config, reason=reason)
    except Exception as exc:
        directory = _auto_recovery_dir(config)
        failure = {
            "schema_version": 1,
            "healthy": False,
            "last_backup": "",
            "reason": str(reason or "unspecified"),
            "translation_records": 0,
            "size_bytes": 0,
            "path": str(directory / AUTO_RECOVERY_LATEST_FILE),
            "project_name": config.name,
            "project_root": str(Path(config.root).expanduser().resolve()),
            "contains_audio": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        try:
            directory.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                directory / AUTO_RECOVERY_STATUS_FILE,
                json.dumps(failure, ensure_ascii=False, indent=2),
            )
        except Exception:
            pass
        return {
            "enabled": True,
            "has_backup": (directory / AUTO_RECOVERY_LATEST_FILE).is_file(),
            "healthy": False,
            "last_backup": "",
            "reason": failure["reason"],
            "translation_records": 0,
            "size_bytes": 0,
            "path": failure["path"],
            "directory": str(directory),
            "error": failure["error"],
            "contains_audio": False,
        }


def import_text_recovery(config: ProjectConfig, recovery_text: str) -> dict[str, Any]:
    raw = str(recovery_text or "").encode("utf-8")
    if not raw:
        raise ValueError("Recovery package is empty")
    if len(raw) > MAX_RECOVERY_BYTES:
        raise ValueError(f"Recovery package exceeds {MAX_RECOVERY_BYTES} bytes")

    package = _read_json_bytes(raw, "package")
    if package.get("format") != RECOVERY_FORMAT:
        raise ValueError("Not an HSR Voice text recovery package")
    if package.get("schema_version") != RECOVERY_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported recovery schema: {package.get('schema_version')}"
        )
    raw_files = package.get("files", {})
    if not isinstance(raw_files, dict):
        raise ValueError("Recovery package files must be an object")

    decoded: dict[str, bytes] = {}
    total = 0
    for logical_name, entry in raw_files.items():
        name = str(logical_name)
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(f"Unsafe recovery path: {name}")
        data = _decode_file(entry, name)
        total += len(data)
        if total > MAX_RECOVERY_BYTES:
            raise ValueError("Recovery payload is too large")
        decoded[name] = data

    checkpoint_data = decoded.get("state/.translation_checkpoint.json")
    if checkpoint_data is None:
        raise ValueError("Recovery package contains no translation checkpoint")
    imported_checkpoint = _read_json_bytes(
        checkpoint_data, "state/.translation_checkpoint.json"
    )
    imported_identity = _checkpoint_identity(imported_checkpoint)
    if imported_identity is None:
        raise ValueError("Unsupported translation checkpoint schema")
    records = imported_checkpoint.get("records", {})
    if not isinstance(records, dict):
        raise ValueError("Translation checkpoint records must be an object")

    state = resolve_project_path(config, config.state_dir)
    index = resolve_project_path(config, config.index_csv)
    if state is None:
        raise ValueError("Project state directory is not configured")
    state.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    imports_dir = state / "recovery_imports"
    imports_dir.mkdir(parents=True, exist_ok=True)
    preserved_package = imports_dir / f"text-recovery-{stamp}.hsrbackup"
    atomic_write_text(preserved_package, raw.decode("utf-8"))

    current_rows: dict[str, dict[str, str]] = {}
    if index is not None and index.is_file():
        current_rows = {
            Path(str(row.get("filename", ""))).name: row
            for row in normalize_index(index)
        }

    matching_now = 0
    changed_now = 0
    not_present_now = 0
    for row_id, saved in records.items():
        filename = Path(str(row_id)).name
        current = current_rows.get(filename)
        if current is None:
            not_present_now += 1
            continue
        if not isinstance(saved, dict):
            changed_now += 1
            continue
        expected = _translation_input_fingerprint(
            current,
            str(config.source_text_language or "en"),
            str(config.target_language or "zh-CN"),
        )
        saved_input = str(saved.get("input_sha256", "") or "")
        if saved_input and saved_input == expected:
            matching_now += 1
        elif (
            not saved_input
            and str(config.source_text_language or "en") == "en"
            and str(config.target_language or "zh-CN") == "zh-CN"
            and not str(current.get("reference_text", "") or "")
            and str(saved.get("english_sha256", "") or "")
            == hashlib.sha256(str(current.get("english", "")).encode("utf-8")).hexdigest()
        ):
            matching_now += 1
        else:
            changed_now += 1

    imported_records = 0
    skipped_existing = 0
    active_checkpoint = state / ".translation_checkpoint.json"
    checkpoint_conflict = False
    active_identity: dict[str, str] | None = None

    if active_checkpoint.is_file():
        try:
            current_payload = _read_json_bytes(
                active_checkpoint.read_bytes(), str(active_checkpoint)
            )
            active_identity = _checkpoint_identity(current_payload)
        except ValueError:
            current_payload = None
        if current_payload is None or active_identity != imported_identity:
            checkpoint_conflict = True
        else:
            current_records = (
                dict(current_payload["records"])
                if isinstance(current_payload.get("records"), dict)
                else {}
            )
            for row_id, saved in records.items():
                existing = current_records.get(str(row_id))
                if isinstance(existing, dict) and str(existing.get("chinese", "")).strip():
                    skipped_existing += 1
                    continue
                if not isinstance(saved, dict) or not str(saved.get("chinese", "")).strip():
                    continue
                current_records[str(row_id)] = saved
                imported_records += 1
            current_payload["records"] = current_records
            atomic_write_text(
                active_checkpoint,
                json.dumps(current_payload, ensure_ascii=False, indent=2),
            )
    else:
        atomic_write_text(
            active_checkpoint,
            json.dumps(imported_checkpoint, ensure_ascii=False, indent=2),
        )
        imported_records = sum(
            1
            for saved in records.values()
            if isinstance(saved, dict) and str(saved.get("chinese", "")).strip()
        )
        active_identity = imported_identity

    return {
        "package_project": str((package.get("project") or {}).get("name", "")),
        "package_records": len(records),
        "package_route": imported_identity,
        "active_route": active_identity,
        "matching_now": matching_now,
        "changed_now": changed_now,
        "not_present_now": not_present_now,
        "imported_records": imported_records,
        "skipped_existing": skipped_existing,
        "checkpoint_conflict": checkpoint_conflict,
        "preserved_package": str(preserved_package),
        "contains_audio": False,
        "message": (
            "恢复包已校验并保存。"
            + (
                " 当前项目存在不同翻译路线的检查点，因此未覆盖它；恢复包仍已保存在 recovery_imports。"
                if checkpoint_conflict
                else f" 已恢复/合并 {imported_records} 条翻译记录。"
            )
        ),
    }
