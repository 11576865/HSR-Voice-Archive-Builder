"""Cross-process exclusion for exported ASS window-pass writers.

Do not delete the lock file: unlinking it permits two independent inodes to
be locked at once. A stable file in the SAME resolved output directory guards
checkpoint reads, FFmpeg rendering and receipt/checkpoint commits together.
"""
from __future__ import annotations

import errno
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOCK_NAME = ".ass-render-coverage-writer.lock"


@contextmanager
def exclusive_coverage_writer(
    output_dir: Path, *, cancel_event: threading.Event | None = None,
) -> Iterator[None]:
    """Fail fast if another process owns this output's coverage writer lock.

    Linux/Termux: flock(LOCK_EX | LOCK_NB). Windows: msvcrt.locking one
    permanent byte with LK_NBLCK. These OS-held locks release on process
    death and do not depend on guessing whether a PID is still alive.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("ASS coverage writer canceled before lock acquisition")
    directory = Path(output_dir).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("ASS coverage output path must be an existing directory")
    path = directory / LOCK_NAME
    if path.is_symlink():
        raise ValueError("ASS coverage lock must not be a symlink")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    locked = False
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or path.is_symlink()
                or (opened.st_dev, opened.st_ino) !=
                   (path.stat().st_dev, path.stat().st_ino)):
            raise ValueError("ASS coverage lock path is unsafe or was replaced")
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if exc.errno in {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES}:
                    raise RuntimeError(
                        "ASS coverage is already being written by another process"
                    ) from exc
                raise
        elif os.name == "nt":
            import msvcrt
            if opened.st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError as exc:
                if (getattr(exc, "winerror", None) in {33, 36}
                        or exc.errno in {errno.EACCES, errno.EAGAIN}):
                    raise RuntimeError(
                        "ASS coverage is already being written by another process"
                    ) from exc
                raise
        else:
            raise RuntimeError("No supported cross-process ASS coverage file lock")
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("ASS coverage writer canceled after lock acquisition")
        yield
    finally:
        try:
            if locked:
                if os.name == "posix":
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
                elif os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
