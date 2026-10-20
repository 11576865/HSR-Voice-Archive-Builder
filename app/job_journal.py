"""Single-writer, multi-process-safe journal for the local job registry.

The journal retains one JSON format for backward compatibility. A per-process
lease identifies running owners, avoiding incorrect "interrupted" transitions
when a second HSR server imports this module.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .project import STATE_DIR, atomic_write_json
from .project_writer_lock import lock_project_writer

SESSION_ID = uuid.uuid4().hex
# Keep the session lease open for the life of this Python interpreter.
_OWN_LEASE = lock_project_writer(str(STATE_DIR / "job-sessions" / SESSION_ID))
_OWN_LEASE.__enter__()


def _session_lease(owner_id: str):
    return lock_project_writer(str(STATE_DIR / "job-sessions" / owner_id))


def session_is_alive(owner_id: object) -> bool:
    if (not isinstance(owner_id, str) or len(owner_id) != 32
            or any(ch not in "0123456789abcdef" for ch in owner_id)):
        return False
    if owner_id == SESSION_ID:
        return True
    try:
        with _session_lease(owner_id):
            return False
    except RuntimeError:
        # The nonblocking lock is held by a live owner.
        return True
    except (OSError, ValueError):
        # Fail closed on uncertain filesystem state: do not claim a running
        # foreign job was interrupted without evidence its owner stopped.
        return True


def read_rows(path: Path, *, strict: bool = False) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Job journal must contain a list")
        if strict and any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
                          for row in data):
            raise ValueError("Job journal contains malformed entries")
        return [row for row in data if isinstance(row, dict)]
    except (OSError, ValueError, TypeError) as exc:
        if strict:
            raise RuntimeError("Shared HSR job journal is unreadable; refusing to overwrite it") from exc
        return []


def _journal_lock(path: Path):
    # Each filename has exactly one durable journal mutex across HSR processes.
    return lock_project_writer(str(path.expanduser().resolve()) + ".journal")


def recover_rows(path: Path, *, limit: int) -> list[dict[str, Any]]:
    """Mark only provably dead owners' jobs interrupted, under journal lock."""
    from datetime import datetime, timezone
    with _journal_lock(path):
        rows = read_rows(path, strict=True)
        changed = False
        for row in rows:
            if row.get("state") not in {"queued", "running"}:
                continue
            if session_is_alive(row.get("owner_session")):
                continue
            row["state"] = "interrupted"
            row["message"] = "上次本地后端退出时任务仍未完成"
            row["finished_at"] = datetime.now(timezone.utc).isoformat()
            if not row.get("error"):
                row["error"] = "Task was interrupted by application/process shutdown."
            changed = True
        if changed:
            atomic_write_json(path, rows[-limit:])
        return rows[-limit:]


def persist_rows(path: Path, owned: list[dict[str, Any]], *, limit: int,
                 deleted: set[str] | None = None) -> None:
    """Merge just this process's jobs with latest on-disk rows under an OS lock."""
    with _journal_lock(path):
        disk = read_rows(path, strict=True)
        merged = {str(row["id"]): row for row in disk if isinstance(row.get("id"), str)}
        for item in owned:
            job_id = str(item["id"])
            if item.get("owner_session") != SESSION_ID:
                raise ValueError("Refusing to overwrite another HSR process's job")
            merged[job_id] = item
        for job_id in deleted or ():
            merged.pop(job_id, None)
        # Do not prune another running process's only durable state. Completed
        # history remains bounded; active entries may temporarily exceed limit.
        ordered = sorted(merged.values(), key=lambda x: (str(x.get("created_at", "")), str(x["id"])))
        active = [x for x in ordered if x.get("state") in {"queued", "running"}]
        completed = [x for x in ordered if x.get("state") not in {"queued", "running"}]
        keep_completed = max(0, limit - len(active))
        surviving = sorted(active + completed[-keep_completed:] if keep_completed else active,
                           key=lambda x: (str(x.get("created_at", "")), str(x["id"])))
        atomic_write_json(path, surviving)
