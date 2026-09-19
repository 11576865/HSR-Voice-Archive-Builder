from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable


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

    def runner() -> None:
        with _LOCK:
            job.state = "running"
            job.message = "正在处理"
            job.started_at = now()
        try:
            result = fn()
            with _LOCK:
                job.result = result
                job.state = "succeeded"
                job.message = "完成"
        except Exception as exc:
            with _LOCK:
                job.state = "failed"
                job.message = "失败"
                job.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
        finally:
            with _LOCK:
                job.finished_at = now()

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
