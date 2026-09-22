from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .builder import atomic_write_text
from .credentials import translation_identity
from .project import ProjectConfig, resolve_project_path
from .schema import normalize_index

RECOVERY_FORMAT = "hsr-voice-text-recovery"
RECOVERY_SCHEMA_VERSION = 1
MAX_RECOVERY_BYTES = 32 * 1024 * 1024

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


def _current_route(config: ProjectConfig) -> dict[str, str]:
    provider, base_url = translation_identity()
    return {
        "provider": provider,
        "base_url": base_url,
        "model": str(config.translation_model or ""),
        "source_language": str(config.source_text_language or "en"),
        "target_language": str(config.target_language or "zh-CN"),
    }


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

    current_route = _current_route(config)
    route_compatible = imported_identity == current_route

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

    if route_compatible:
        current_payload: dict[str, Any] | None = None
        if active_checkpoint.is_file():
            try:
                current_payload = _read_json_bytes(
                    active_checkpoint.read_bytes(), str(active_checkpoint)
                )
            except ValueError:
                current_payload = None

        if current_payload is not None and _checkpoint_identity(current_payload) != current_route:
            checkpoint_conflict = True
        else:
            current_records: dict[str, Any] = {}
            if current_payload is not None and isinstance(current_payload.get("records"), dict):
                current_records = dict(current_payload["records"])
            for row_id, saved in records.items():
                existing = current_records.get(str(row_id))
                if isinstance(existing, dict) and str(existing.get("chinese", "")).strip():
                    skipped_existing += 1
                    continue
                if not isinstance(saved, dict) or not str(saved.get("chinese", "")).strip():
                    continue
                current_records[str(row_id)] = saved
                imported_records += 1

            payload = {
                "schema_version": 3,
                **current_route,
                "records": current_records,
            }
            atomic_write_text(
                active_checkpoint,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )

    return {
        "package_project": str((package.get("project") or {}).get("name", "")),
        "package_records": len(records),
        "route_compatible": route_compatible,
        "current_route": current_route,
        "package_route": imported_identity,
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
                f" 已向当前翻译检查点合并 {imported_records} 条记录。"
                if route_compatible and not checkpoint_conflict
                else " 当前 API 路由/模型或现有检查点不兼容，因此未改写活动翻译检查点。"
            )
        ),
    }
