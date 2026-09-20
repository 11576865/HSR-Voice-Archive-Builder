from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGE_SCHEMA_VERSION = 1
SOFTWARE_STAGE_VERSION = "0.9-B"


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_fingerprint(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    if resolved.is_symlink():
        raise ValueError(f"Stage input cannot be a symbolic link: {resolved}")
    if resolved.is_file():
        stat = resolved.stat()
        return {"kind": "file", "size_bytes": stat.st_size, "sha256": _sha256_file(resolved)}
    if not resolved.is_dir():
        raise ValueError(f"Unsupported stage input: {resolved}")

    tree = hashlib.sha256()
    count = 0
    for child in sorted(resolved.rglob("*"), key=lambda item: item.as_posix()):
        if child.is_symlink():
            raise ValueError(f"Stage input directory contains a symbolic link: {child}")
        if not child.is_file():
            continue
        relative = child.relative_to(resolved).as_posix()
        stat = child.stat()
        digest = _sha256_file(child)
        tree.update(relative.encode("utf-8"))
        tree.update(b"\0")
        tree.update(str(stat.st_size).encode("ascii"))
        tree.update(b"\0")
        tree.update(digest.encode("ascii"))
        tree.update(b"\n")
        count += 1
    return {"kind": "directory", "file_count": count, "tree_sha256": tree.hexdigest()}


def build_fingerprint(inputs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    canonical = {"software_stage_version": SOFTWARE_STAGE_VERSION, **inputs}
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), canonical


def stage_path(out_dir: Path, filename: str) -> Path:
    return out_dir / "stages" / filename


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_stage(
    out_dir: Path,
    filename: str,
    stage: str,
    input_fingerprint: str,
    payload: dict[str, Any],
    *,
    artifacts: list[Path] | None = None,
) -> Path:
    artifact_rows: list[dict[str, Any]] = []
    for artifact in artifacts or []:
        resolved = artifact.resolve()
        relative = resolved.relative_to(out_dir.resolve()).as_posix()
        artifact_rows.append({
            "path": relative,
            "size_bytes": resolved.stat().st_size,
            "sha256": _sha256_file(resolved),
        })
    destination = stage_path(out_dir, filename)
    _atomic_write_json(destination, {
        "schema_version": STAGE_SCHEMA_VERSION,
        "software_stage_version": SOFTWARE_STAGE_VERSION,
        "stage": stage,
        "input_fingerprint": input_fingerprint,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": artifact_rows,
        "payload": payload,
    })
    return destination


def load_stage(
    out_dir: Path,
    filename: str,
    stage: str,
    input_fingerprint: str,
) -> dict[str, Any] | None:
    path = stage_path(out_dir, filename)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if (
            document.get("schema_version") != STAGE_SCHEMA_VERSION
            or document.get("software_stage_version") != SOFTWARE_STAGE_VERSION
            or document.get("stage") != stage
            or document.get("input_fingerprint") != input_fingerprint
            or not isinstance(document.get("payload"), dict)
        ):
            return None
        for row in document.get("artifacts", []):
            relative = Path(str(row["path"]))
            if relative.is_absolute() or ".." in relative.parts:
                return None
            artifact = (out_dir / relative).resolve()
            artifact.relative_to(out_dir.resolve())
            if (
                not artifact.is_file()
                or artifact.stat().st_size != int(row["size_bytes"])
                or _sha256_file(artifact) != row["sha256"]
            ):
                return None
        return document["payload"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
