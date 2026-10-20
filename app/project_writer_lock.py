"""Cross-process, non-blocking writer guard for one HSR project.

A lock file is intentionally retained: unlinking a locked inode could let
another process lock a replacement and concurrently write the same project.
"""
from __future__ import annotations

import errno
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .project import STATE_DIR


def _lock_path(project_root: str) -> Path:
    root = str(Path(project_root).expanduser().resolve())
    digest = hashlib.sha256(os.path.normcase(root).encode("utf-8")).hexdigest()
    return STATE_DIR / "project-writer-locks" / (digest + ".lock")


@contextmanager
def lock_project_writer(project_root: str) -> Iterator[None]:
    """Fail closed on another process's writer; release even on job failure.

    Windows uses msvcrt 1-byte range locks; POSIX uses flock. The gate
    protects cooperating HSR processes using the same OS-user state directory.
    It does not serialize arbitrary external editors or different OS users.
    """
    target = str(project_root or "").strip()
    if not target:
        raise ValueError("Project root is required for a writer lock")
    path = _lock_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Unsafe HSR writer lock path")
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\\0")
            handle.flush()
        handle.seek(0)
        acquired = False
        try:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise RuntimeError(
                        "同一项目正在由另一个 HSR 进程写入；请等待其完成，"
                        "不要同时从两个服务修改 FLAC/ASS/MKV"
                    ) from exc
                raise
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
