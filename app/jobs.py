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


def create_job(kind: str, fn: Callable[[], Any]) -> Job:
    job = Job(
        id=uuid.uuid4().hex,
        kind=kind,
        state="queued",
        message="等待执行",
        created_at=now(),
    )
    with _LOCK:
        _JOBS[job.id] = job
        _persist_locked()

    def runner() -> None:
        with _LOCK:
            job.state = "running"
            job.message = "正在处理"
            job.started_at = now()
            _persist_locked()
        try:
            result = fn()
            with _LOCK:
                job.result = result
                job.state = "succeeded"
                job.message = "完成"
                job.finished_at = now()
                _persist_locked()
        except Exception as exc:
            with _LOCK:
                job.state = "failed"
                job.message = "失败"
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
