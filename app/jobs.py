from __future__ import annotations

from contextlib import contextmanager, nullcontext
import logging
import threading
import traceback
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from .project import STATE_DIR
from .job_journal import SESSION_ID, compact_tombstones, persist_rows, recover_snapshot, recover_rows
from .project_writer_lock import lock_project_writer

JOBS_FILE = STATE_DIR / "jobs.json"
MAX_PERSISTED_JOBS = 100
BUILD_KINDS = {"build", "quick-build"}
# These jobs produce or invalidate files in one project's output directory.
# Concurrent writers can overwrite a newer, validated MKV/ASS/FLAC artifact.
PROJECT_OUTPUT_WRITERS = BUILD_KINDS | {
    "ass-export", "ass-external", "black-video-export", "archive-bundle-export",
    "artifact-extract", "remote-update-apply", "locale-recovery",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    kind: str
    state: str
    message: str
    created_at: str
    started_at: str = ""
    finished_at: str = ""
    result: Any = None
    error: str = ""
    phase: str = ""
    progress_current: int = 0
    progress_total: int = 0
    project_root: str = ""
    project_name: str = ""
    owner_session: str = ""


_LOCK = threading.Lock()
_JOBS: dict[str, Job] = {}
# Synchronous HTTP exports must reserve the same writer slot as async jobs.
_RESERVED_PROJECT_WRITERS: dict[str, str] = {}
_DELETED_JOB_IDS: set[str] = set()


def _persist_locked() -> None:
    # Persist only this process's state. Never replace another live HSR
    # instance's jobs with a stale in-memory copy of the shared JSON array.
    owned = [asdict(job) for job in _JOBS.values()
             if job.owner_session == SESSION_ID]
    persist_rows(JOBS_FILE, owned, limit=MAX_PERSISTED_JOBS,
                 deleted=_DELETED_JOB_IDS)
    # Durable sidecar now owns these deletions; do not reassert IDs later
    # after another process has proven their producer exited and compacted.
    _DELETED_JOB_IDS.clear()


def _refresh_foreign_locked() -> None:
    """Refresh other processes' jobs, preserving this process's live objects."""
    rows, deleted = recover_snapshot(JOBS_FILE, limit=MAX_PERSISTED_JOBS)
    fresh: dict[str, Job] = {}
    for item in rows:
        try:
            candidate = Job(**item)
        except (TypeError, ValueError):
            continue
        if candidate.owner_session != SESSION_ID:
            fresh[candidate.id] = candidate
    for job_id, job in list(_JOBS.items()):
        if job_id in deleted and job.state not in {"queued", "running"}:
            _JOBS.pop(job_id, None)
        elif job.owner_session != SESSION_ID and job_id not in fresh:
            _JOBS.pop(job_id, None)
    _JOBS.update(fresh)


def _load_previous() -> None:
    try:
        rows = recover_rows(JOBS_FILE, limit=MAX_PERSISTED_JOBS)
        for item in rows:
            try:
                job = Job(**item)
            except (TypeError, ValueError):
                continue
            _JOBS[job.id] = job
    except Exception:
        # Preserve the original journal on corruption; subsequent writes
        # fail closed rather than silently replacing potentially valid jobs.
        _JOBS.clear()
        return
    # One best-effort startup sweep is enough to discard provably expired
    # v2 tombstones without expensive OS-lease probing on every status poll.
    # If disk maintenance fails, preserve the existing job view and ledger.
    try:
        compact_tombstones(JOBS_FILE, limit=MAX_PERSISTED_JOBS)
    except (OSError, RuntimeError) as exc:
        logging.getLogger(__name__).warning(
            "HSR job tombstone compaction deferred: %s", exc
        )


_load_previous()


def _active_build_locked() -> Job | None:
    _refresh_foreign_locked()
    for job in reversed(list(_JOBS.values())):
        if job.kind in BUILD_KINDS and job.state in {"queued", "running"}:
            return job
    return None


def active_build_job() -> dict[str, Any] | None:
    with _LOCK:
        job = _active_build_locked()
        return asdict(job) if job else None


def assert_no_active_build() -> None:
    with _LOCK:
        job = _active_build_locked()
        if job is not None:
            raise RuntimeError(
                "已有构建任务正在运行，请等待它完成后再开始新的构建"
            )


@contextmanager
def reserve_project_output_writer(project_root: str, *, kind: str = "ass-refresh") -> Iterator[None]:
    """Atomically exclude asynchronous jobs and other synchronous export requests.

    Acquiring this reservation happens BEFORE dispatching expensive work to a
    threadpool. It is released even when rendering fails or the request exits.
    The reservation also holds a non-blocking OS file lock for its lifetime.
    """
    target = str(project_root or "").strip()
    if not target:
        raise ValueError("Project root is required for an output writer")
    with _LOCK:
        _refresh_foreign_locked()
        incumbent = _RESERVED_PROJECT_WRITERS.get(target)
        if not incumbent:
            incumbent = next(
                (existing.kind for existing in _JOBS.values()
                 if existing.project_root == target
                 and existing.kind in PROJECT_OUTPUT_WRITERS
                 and existing.state in {"queued", "running"}),
                None,
            )
        if incumbent:
            raise RuntimeError(
                f"同一项目已有输出任务正在运行（{incumbent}），请等待完成，"
                "避免 FLAC/ASS/MKV 成品被并发覆盖"
            )
        # Reserve across cooperating processes before exposing a writer slot.
        # Hold the in-process mutex across acquisition to prevent local races.
        interprocess = lock_project_writer(target)
        interprocess.__enter__()
        _RESERVED_PROJECT_WRITERS[target] = kind
    try:
        yield
    finally:
        with _LOCK:
            _RESERVED_PROJECT_WRITERS.pop(target, None)
        interprocess.__exit__(None, None, None)


def assert_project_idle(project_root: str) -> None:
    target = str(project_root or "").strip()
    if not target:
        return
    with _LOCK:
        _refresh_foreign_locked()
        if target in _RESERVED_PROJECT_WRITERS:
            raise RuntimeError(
                f"项目「{target}」的字幕输出仍在运行，不能同时修改或删除该项目"
            )
        for job in _JOBS.values():
            if job.project_root == target and job.state in {"queued", "running"}:
                raise RuntimeError(
                    f"项目「{job.project_name or target}」仍有任务正在运行，请等待任务结束后再删除"
                )

    # Probe other HSR instances before treating this project as idle.
    with lock_project_writer(target):
        pass


def create_job(
    kind: str,
    fn: Callable[..., Any],
    *,
    with_progress: bool = False,
    project_root: str = "",
    project_name: str = "",
) -> Job:
    job = Job(
        id=uuid.uuid4().hex,
        kind=kind,
        state="queued",
        message="等待执行",
        created_at=now(),
        project_root=str(project_root or ""),
        project_name=str(project_name or ""),
        owner_session=SESSION_ID,
    )
    with _LOCK:
        _refresh_foreign_locked()
        if kind in BUILD_KINDS:
            active = _active_build_locked()
            if active is not None:
                raise RuntimeError(
                    "已有构建任务正在运行，请等待它完成后再开始新的构建"
                )
        if kind in PROJECT_OUTPUT_WRITERS and job.project_root:
            reserved = _RESERVED_PROJECT_WRITERS.get(job.project_root)
            if reserved:
                raise RuntimeError(
                    "同一项目已有输出任务正在运行（" + reserved +
                    "），请等待完成，避免 FLAC/ASS/MKV 成品被并发覆盖"
                )
            for existing in _JOBS.values():
                if (existing.project_root == job.project_root
                        and existing.kind in PROJECT_OUTPUT_WRITERS
                        and existing.state in {"queued", "running"}):
                    raise RuntimeError(
                        "同一项目已有输出任务正在运行（"
                        + existing.kind
                        + "），请等待完成，避免 FLAC/ASS/MKV 成品被并发覆盖"
                    )
        _JOBS[job.id] = job
        try:
            _persist_locked()
        except Exception:
            # A failed journal admission must not leave an unstarted queued
            # job that blocks future builds for this server process.
            _JOBS.pop(job.id, None)
            raise

    def report_progress(
        phase: str,
        message: str,
        current: int = 0,
        total: int = 0,
    ) -> None:
        with _LOCK:
            if job.state not in {"queued", "running"}:
                return
            job.phase = str(phase or "")
            job.message = str(message or "正在处理")
            job.progress_current = max(0, int(current))
            job.progress_total = max(0, int(total))
            _persist_locked()

    def runner() -> None:
        # A durable transition to "running" is required before executing any
        # side effects. Failing here used to escape the daemon thread outside
        # the exception handler and leave an in-memory ghost "running" job.
        try:
            with _LOCK:
                job.state = "running"
                job.message = "正在准备"
                job.started_at = now()
                _persist_locked()
        except Exception as exc:
            with _LOCK:
                job.state = "failed"
                job.message = "构建失败" if kind in BUILD_KINDS else "失败"
                job.finished_at = now()
                job.error = (
                    f"Task was not started because the running-state journal "
                    f"could not be saved: {type(exc).__name__}: {exc}"
                )
                try:
                    _persist_locked()
                except Exception:
                    # The journal may remain unavailable. Keep this process's
                    # state terminal and let recovery mark any disk-side queued
                    # state interrupted once the session ends.
                    logging.getLogger(__name__).exception(
                        "Unable to publish failed HSR job startup: %s", job.id
                    )
            return
        try:
            guard = (lock_project_writer(job.project_root)
                     if kind in PROJECT_OUTPUT_WRITERS and job.project_root
                     else nullcontext())
            with guard:
                result = fn(report_progress) if with_progress else fn()
            with _LOCK:
                job.result = result
                job.state = "succeeded"
                job.phase = "done"
                job.message = "构建完成" if kind in BUILD_KINDS else "完成"
                if kind in BUILD_KINDS:
                    job.progress_current = max(job.progress_total, 1)
                    job.progress_total = max(job.progress_total, 1)
                job.finished_at = now()
                _persist_locked()
        except Exception as exc:
            with _LOCK:
                waiting = getattr(exc, "job_state", "") == "awaiting_input"
                job.state = "awaiting_input" if waiting else "failed"
                job.message = "等待用户提交人工修订" if waiting else ("构建失败" if kind in BUILD_KINDS else "失败")
                if waiting:
                    job.result = getattr(exc, "job_result", None)
                job.finished_at = now()
                job.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
                _persist_locked()

    # Jobs intentionally remain daemon threads so closing the local application
    # is not blocked by a long FFmpeg/network operation. The persistent journal
    # marks unfinished work as "interrupted" on the next start.
    threading.Thread(target=runner, name=f"hsr-job-{job.id[:8]}", daemon=True).start()
    return job


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        _refresh_foreign_locked()
        job = _JOBS.get(job_id)
        return asdict(job) if job else None


def recent_jobs(limit: int = 20) -> list[dict[str, Any]]:
    with _LOCK:
        _refresh_foreign_locked()
        values = sorted(_JOBS.values(), key=lambda job: (job.created_at, job.id))[-limit:]
        return [asdict(job) for job in reversed(values)]


def compact_job_history_tombstones() -> dict[str, int]:
    """Explicit low-risk maintenance for proven-dead job owners only."""
    with _LOCK:
        result = compact_tombstones(JOBS_FILE, limit=MAX_PERSISTED_JOBS)
        _refresh_foreign_locked()
        return result


def delete_project_jobs(project_root: str) -> int:
    """Remove completed/history records owned by a deleted project."""
    target = str(project_root or "").strip()
    if not target:
        return 0
    with _LOCK:
        _refresh_foreign_locked()
        doomed = [
            job_id for job_id, job in _JOBS.items()
            if job.project_root == target and job.state not in {"queued", "running"}
        ]
        previous = {job_id: _JOBS[job_id] for job_id in doomed}
        for job_id in doomed:
            _JOBS.pop(job_id, None)
            _DELETED_JOB_IDS.add(job_id)
        if doomed:
            try:
                _persist_locked()
            except Exception:
                _JOBS.update(previous)
                _DELETED_JOB_IDS.difference_update(doomed)
                raise
        return len(doomed)
