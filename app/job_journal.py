"""Single-writer, multi-process-safe journal for the local job registry.

The journal retains one JSON format for backward compatibility. A per-process
lease identifies running owners, avoiding incorrect "interrupted" transitions
when a second HSR server imports this module.
"""
from __future__ import annotations

import atexit
import time
from contextlib import contextmanager
import uuid
from pathlib import Path
from typing import Any

from .project import STATE_DIR, atomic_write_json
from .project_writer_lock import lock_project_writer

SESSION_ID = uuid.uuid4().hex
# Keep the session lease open for the life of this Python interpreter.
_OWN_LEASE = lock_project_writer(str(STATE_DIR / "job-sessions" / SESSION_ID))
_OWN_LEASE.__enter__()
# Release explicitly before interpreter module teardown; OS still releases
# automatically on crashes / forced process termination.
atexit.register(_OWN_LEASE.__exit__, None, None, None)


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


@contextmanager
def _journal_lock(path: Path):
    """Wait briefly for short journal writes; do not fail-fast like media jobs.

    Unlike the project output writer fence, job-journal edits are short,
    independently mergeable transactions. A transient lock collision must
    not crash a running job or spuriously mark its owner interrupted.
    """
    target = str(path.expanduser().resolve()) + ".journal"
    deadline = time.monotonic() + 15.0
    while True:
        gate = lock_project_writer(target)
        try:
            gate.__enter__()
        except RuntimeError as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError("HSR shared job journal lock timed out") from exc
            time.sleep(0.02)
        else:
            break
    try:
        yield
    finally:
        gate.__exit__(None, None, None)


def _tombstone_path(path: Path) -> Path:
    # A stable sidecar allows old jobs.json consumers to keep the JSON-list
    # format, while a deleted ID remains authoritative across live processes.
    return path.with_name(path.name + ".deleted.json")


def _valid_session(owner_id: object) -> bool:
    return (
        isinstance(owner_id, str) and len(owner_id) == 32
        and all(c in "0123456789abcdef" for c in owner_id)
    )


def _read_ledger(path: Path) -> tuple[set[str], dict[str, str]]:
    """Accept v1 IDs conservatively; only v2 records carry pruneable owners.

    A legacy v1 tombstone cannot safely be compacted: its owner is unknown,
    and a still-live writer may retain the completed job in memory.
    """
    location = _tombstone_path(path)
    if not location.is_file():
        return set(), {}
    try:
        import json
        data = json.loads(location.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") not in (1, 2):
            raise ValueError("Invalid job deletion ledger version")
        ids = data.get("job_ids")
        if (not isinstance(ids, list)
                or any(not isinstance(item, str) or not item for item in ids)
                or len(ids) != len(set(ids))):
            raise ValueError("Invalid deleted job IDs")
        # Version 1 readers ignore unknown fields; keep this layout for
        # cross-version HSR instances sharing the same user state directory.
        # Accept the short-lived v2 writer from PR #199 for migration.
        owners = data.get("owner_sessions", {})
        if (not isinstance(owners, dict)
                or any(job_id not in ids or not _valid_session(session)
                       for job_id, session in owners.items())):
            raise ValueError("Invalid tombstone owner metadata")
        return set(ids), dict(owners)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(
            "Shared HSR job deletion ledger is unreadable; refusing to resurrect jobs"
        ) from exc


def _read_deleted(path: Path) -> set[str]:
    # Keep the public/internal v1 set interface for existing consumers.
    return _read_ledger(path)[0]


def _write_ledger(path: Path, deleted: set[str], owners: dict[str, str]) -> None:
    atomic_write_json(
        _tombstone_path(path),
        {"schema_version": 1, "job_ids": sorted(deleted),
         "owner_sessions": {k: owners[k] for k in sorted(owners)}},
    )


def compact_tombstones(path: Path, *, limit: int) -> dict[str, int]:
    """Safely compact only tombstones whose exact owner session is dead.

    Under the shared journal OS lock, remove any tombstoned raw JSON rows
    FIRST, then publish the smaller sidecar. A crash between those atomic
    writes retains the old sidecar, which is safe to retry. Never compact
    v1/unknown-owner or live-owner tombstones.

    This is explicit maintenance rather than a timer or a poll-side effect.
    """
    with _journal_lock(path):
        disk = read_rows(path, strict=True)
        deleted, owners = _read_ledger(path)
        removable = {job_id for job_id, session in owners.items()
                     if job_id in deleted and not session_is_alive(session)}
        if not removable:
            # A short-lived PR #199 main build wrote schema v2, which older
            # still-running HSR servers reject. Downgrade in-place even when
            # the owner is still alive, without dropping any tombstones.
            location = _tombstone_path(path)
            if location.is_file():
                import json
                if json.loads(location.read_text(encoding="utf-8")).get("schema_version") == 2:
                    _write_ledger(path, deleted, owners)
            return {"removed": 0, "retained": len(deleted)}
        # The old sidecar still protects deletions until *after* publication
        # of the journal without any row it formerly masked.
        kept = _trim_rows([row for row in disk if row["id"] not in deleted], limit)
        atomic_write_json(path, kept)
        remaining = deleted - removable
        _write_ledger(path, remaining, {k: v for k, v in owners.items()
                                        if k in remaining})
        return {"removed": len(removable), "retained": len(remaining)}


def _trim_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda x: (str(x.get("created_at", "")), str(x["id"])))
    active = [x for x in ordered if x.get("state") in {"queued", "running"}]
    completed = [x for x in ordered if x.get("state") not in {"queued", "running"}]
    keep = max(0, limit - len(active))
    return sorted(active + (completed[-keep:] if keep else []),
                  key=lambda x: (str(x.get("created_at", "")), str(x["id"])))


def recover_snapshot(path: Path, *, limit: int) -> tuple[list[dict[str, Any]], set[str]]:
    """Atomically reconcile peer liveness and journal deletion tombstones.

    Called by status polling too, not solely when a new Python server boots.
    Unknown/permission-denied owner leases are intentionally treated as alive.
    """
    from datetime import datetime, timezone
    with _journal_lock(path):
        disk = read_rows(path, strict=True)
        deleted = _read_deleted(path)
        rows = [row for row in disk if row["id"] not in deleted]
        changed = len(rows) != len(disk)
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
        kept = _trim_rows(rows, limit)
        if changed or len(kept) != len(rows):
            atomic_write_json(path, kept)
        return kept, deleted


def recover_rows(path: Path, *, limit: int) -> list[dict[str, Any]]:
    """Legacy-compatible recovery wrapper; returns the journal's list rows."""
    rows, _deleted = recover_snapshot(path, limit=limit)
    return rows


def persist_rows(path: Path, owned: list[dict[str, Any]], *, limit: int,
                 deleted: set[str] | None = None) -> None:
    """Merge local jobs without reintroducing deleted history.

    New tombstones capture only authenticated (valid) producer session IDs;
    unknown legacy owners are never considered safe for compaction. The
    deletion ledger still publishes *before* the replacement jobs.json.
    """
    with _journal_lock(path):
        disk = read_rows(path, strict=True)
        existing_deleted, owners = _read_ledger(path)
        requests = set(deleted or ())
        if requests:
            active = {row["id"] for row in disk
                      if row["id"] in requests
                      and row.get("state") in {"queued", "running"}}
            if active:
                raise RuntimeError("Cannot delete history while a selected job is still active")
        all_deleted = existing_deleted | requests
        if all_deleted != existing_deleted:
            for row in disk:
                if row["id"] in requests and row["id"] not in existing_deleted:
                    session = row.get("owner_session")
                    if _valid_session(session):
                        owners[row["id"]] = session
            _write_ledger(path, all_deleted, owners)
        merged = {row["id"]: row for row in disk if row["id"] not in all_deleted}
        for item in owned:
            job_id = str(item["id"])
            if item.get("owner_session") != SESSION_ID:
                raise ValueError("Refusing to overwrite another HSR process's job")
            if job_id not in all_deleted:
                merged[job_id] = item
        atomic_write_json(path, _trim_rows(list(merged.values()), limit))
