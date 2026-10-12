from __future__ import annotations

import json
import threading
import traceback
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .project import STATE_DIR, atomic_write_json

JOBS_FILE = STATE_DIR / "jobs.json"
MAX_PERSISTED_JOBS = 100
BUILD_KINDS = {"build", "quick-build"}
MUTATING_KINDS = BUILD_KINDS | {"remote-update-apply", "remote-update-rollback"}
CANCELABLE_KINDS = {"ass-artifact-evidence"}


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
    cancel_requested: bool = False


_LOCK = threading.Lock()
_JOBS: dict[str, Job] = {}
_CANCEL_EVENTS: dict[str, threading.Event] = {}


def _prune_finished_locked() -> None:
    """Bound in-memory job history without ever discarding live tasks.

    A long exported-ASS window pass may launch thousands of successive
    evidence jobs. The persistent journal is already bounded; the in-memory
    table must not grow indefinitely or slow project exclusion scans.
    """
    if len(_JOBS) <= MAX_PERSISTED_JOBS:
        return
    # Prefer preserving interrupted work (important after an app restart)
    # and tasks requiring human input over routine completed history.
    # If even protected terminal records exceed the history budget,
    # retire their oldest entries in a second pass, never live tasks.
    for protected in (
        {"queued", "running", "interrupted", "awaiting_input"},
        {"queued", "running"},
    ):
        for job_id, job in list(_JOBS.items()):
            if len(_JOBS) <= MAX_PERSISTED_JOBS:
                break
            if job.state not in protected:
                _JOBS.pop(job_id, None)
                _CANCEL_EVENTS.pop(job_id, None)
        if len(_JOBS) <= MAX_PERSISTED_JOBS:
            break


def _persist_locked() -> None:
    _prune_finished_locked()
    values = list(_JOBS.values())
    # Never silently drop a live operation from the crash-recovery journal
    # when concurrent jobs temporarily exceed the normal history limit.
    if len(values) > MAX_PERSISTED_JOBS:
        live_ids = {
            j.id for j in values if j.state in {"queued", "running"}
        }
        slots = max(0, MAX_PERSISTED_JOBS - len(live_ids))
        recent_finished = [j.id for j in values if j.id not in live_ids]
        keep = live_ids | (set(recent_finished[-slots:]) if slots else set())
        values = [j for j in values if j.id in keep]
    atomic_write_json(JOBS_FILE, [asdict(job) for job in values])


def _load_previous() -> None:
    if not JOBS_FILE.is_file():
        return
    try:
        raw = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return
        normal_history_from = max(0, len(raw) - MAX_PERSISTED_JOBS)
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            if index < normal_history_from and item.get("state") not in {"queued", "running"}:
                continue
            job = Job(**item)
            if job.state in {"queued", "running"}:
                job.state = "interrupted"
                job.message = "上次本地后端退出时任务仍未完成"
                job.finished_at = now()
                if not job.error:
                    job.error = "Task was interrupted by application/process shutdown."
            _JOBS[job.id] = job
        _persist_locked()
    except Exception:
        # A damaged journal must never prevent the local app from starting.
        _JOBS.clear()


_load_previous()


def _active_build_locked() -> Job | None:
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


def assert_project_idle(project_root: str) -> None:
    target = str(project_root or "").strip()
    if not target:
        return
    with _LOCK:
        for job in _JOBS.values():
            if job.project_root == target and job.state in {"queued", "running"}:
                raise RuntimeError(
                    f"项目「{job.project_name or target}」仍有任务正在运行，请等待任务结束后再删除"
                )


def create_job(
    kind: str,
    fn: Callable[..., Any],
    *,
    with_progress: bool = False,
    project_root: str = "",
    project_name: str = "",
    with_cancel: bool = False,
) -> Job:
    if with_cancel and kind not in CANCELABLE_KINDS:
        raise ValueError("This job kind cannot be canceled")
    cancel_event = threading.Event() if with_cancel else None
    job = Job(
        id=uuid.uuid4().hex,
        kind=kind,
        state="queued",
        message="等待执行",
        created_at=now(),
        project_root=str(project_root or ""),
        project_name=str(project_name or ""),
    )
    with _LOCK:
        if kind in BUILD_KINDS:
            active = _active_build_locked()
            if active is not None:
                raise RuntimeError(
                    "已有构建任务正在运行，请等待它完成后再开始新的构建"
                )
        if kind in MUTATING_KINDS and job.project_root:
            for active in _JOBS.values():
                if (active.project_root == job.project_root
                        and active.kind in MUTATING_KINDS
                        and active.state in {"queued", "running"}):
                    raise RuntimeError(
                        "同一项目已有构建或增量写入任务，拒绝并发修改来源和索引"
                    )
        if with_cancel and job.project_root:
            if any(other.kind == kind and other.project_root == job.project_root
                   and other.state in {"queued", "running"} for other in _JOBS.values()):
                raise RuntimeError("同一项目已有运行中的 ASS 证据任务")
        _JOBS[job.id] = job
        if cancel_event is not None:
            _CANCEL_EVENTS[job.id] = cancel_event
        _persist_locked()

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
        with _LOCK:
            if cancel_event is not None and cancel_event.is_set():
                job.state = "canceled"
                job.finished_at = now()
                job.message = "已取消"
                _CANCEL_EVENTS.pop(job.id, None)
                _persist_locked()
                return
            job.state = "running"
            job.message = "正在准备"
            job.started_at = now()
            _persist_locked()
        try:
            result = (fn(cancel_event, report_progress) if with_cancel and with_progress
                      else fn(cancel_event) if with_cancel
                      else fn(report_progress) if with_progress else fn())
            with _LOCK:
                # A returned successful result is the durable completion
                # boundary for cancellable ASS proof jobs: the writer has
                # already persisted its receipt/checkpoint before returning.
                # Cancel-after-commit must not say "canceled" while the
                # committed evidence and cursor are visible on disk.
                late_cancel = cancel_event is not None and cancel_event.is_set()
                job.result = result
                job.state = "succeeded"
                job.phase = "done"
                job.message = (
                    "已完成；取消请求抵达时结果已提交"
                    if late_cancel else
                    "构建完成" if kind in BUILD_KINDS else "完成"
                )
                if kind in BUILD_KINDS:
                    job.progress_current = max(job.progress_total, 1)
                    job.progress_total = max(job.progress_total, 1)
                job.finished_at = now()
                _persist_locked()
        except Exception as exc:
            with _LOCK:
                waiting = getattr(exc, "job_state", "") == "awaiting_input"
                canceled = cancel_event is not None and cancel_event.is_set()
                job.state = "canceled" if canceled else "awaiting_input" if waiting else "failed"
                job.message = "已取消" if canceled else "等待用户提交人工修订" if waiting else ("构建失败" if kind in BUILD_KINDS else "失败")
                if waiting:
                    job.result = getattr(exc, "job_result", None)
                job.finished_at = now()
                job.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
                _persist_locked()
        finally:
            with _LOCK:
                _CANCEL_EVENTS.pop(job.id, None)

    # Jobs intentionally remain daemon threads so closing the local application
    # is not blocked by a long FFmpeg/network operation. The persistent journal
    # marks unfinished work as "interrupted" on the next start.
    threading.Thread(target=runner, name=f"hsr-job-{job.id[:8]}", daemon=True).start()
    return job


def cancel_job(job_id: str, *, project_root: str) -> dict[str, Any]:
    """Cooperative cancellation of current-project, allowlisted evidence jobs."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise ValueError("Job not found")
        if not project_root or job.project_root != project_root:
            raise ValueError("Job does not belong to the active project")
        event = _CANCEL_EVENTS.get(job.id)
        if job.kind not in CANCELABLE_KINDS or event is None:
            raise ValueError("Job is not cancelable")
        if job.state not in {"queued", "running"}:
            raise ValueError("Job has already completed")
        job.cancel_requested = True
        job.message = "正在取消渲染检查…"
        event.set()
        _persist_locked()
        return asdict(job)


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return asdict(job) if job else None


def recent_jobs(limit: int = 20) -> list[dict[str, Any]]:
    with _LOCK:
        values = list(_JOBS.values())[-limit:]
        return [asdict(job) for job in reversed(values)]


def delete_project_jobs(project_root: str) -> int:
    """Remove completed/history records owned by a deleted project."""
    target = str(project_root or "").strip()
    if not target:
        return 0
    with _LOCK:
        doomed = [
            job_id for job_id, job in _JOBS.items()
            if job.project_root == target and job.state not in {"queued", "running"}
        ]
        for job_id in doomed:
            _JOBS.pop(job_id, None)
        if doomed:
            _persist_locked()
        return len(doomed)
