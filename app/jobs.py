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


_LOCK = threading.Lock()
_JOBS: dict[str, Job] = {}


def _persist_locked() -> None:
    values = list(_JOBS.values())[-MAX_PERSISTED_JOBS:]
    atomic_write_json(JOBS_FILE, [asdict(job) for job in values])


def _load_previous() -> None:
    if not JOBS_FILE.is_file():
        return
    try:
        raw = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return
        for item in raw[-MAX_PERSISTED_JOBS:]:
            if not isinstance(item, dict):
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
    )
    with _LOCK:
        if kind in BUILD_KINDS:
            active = _active_build_locked()
            if active is not None:
                raise RuntimeError(
                    "已有构建任务正在运行，请等待它完成后再开始新的构建"
                )
        _JOBS[job.id] = job
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
            job.state = "running"
            job.message = "正在准备"
            job.started_at = now()
            _persist_locked()
        try:
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
                job.state = "failed"
                job.message = "构建失败" if kind in BUILD_KINDS else "失败"
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
        job = _JOBS.get(job_id)
        return asdict(job) if job else None


def recent_jobs(limit: int = 20) -> list[dict[str, Any]]:
    with _LOCK:
        values = list(_JOBS.values())[-limit:]
        return [asdict(job) for job in reversed(values)]
