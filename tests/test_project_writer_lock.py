"""Two-process admission evidence for persistent HSR project writers."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import jobs
from app.project_writer_lock import lock_project_writer


class CrossProcessProjectWriterTests(unittest.TestCase):
    def _child_attempt(self, project: Path) -> subprocess.CompletedProcess:
        script = """\
import sys
from app.project_writer_lock import lock_project_writer
try:
    with lock_project_writer(sys.argv[1]):
        print("acquired")
except RuntimeError:
    # CI consoles may use cp1252; the lock result must not require CJK stdout.
    print("writer locked")
    sys.exit(7)
"""
        repo = Path(__file__).resolve().parents[1]
        return subprocess.run(
            [sys.executable, "-c", script, str(project)],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_another_python_process_cannot_mutate_locked_project(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "archive"
            project.mkdir()
            with lock_project_writer(str(project)):
                blocked = self._child_attempt(project)
                self.assertEqual(blocked.returncode, 7, blocked.stderr)
                self.assertIn("writer locked", blocked.stdout)
                with self.assertRaisesRegex(RuntimeError, "另一个 HSR 进程"):
                    jobs.assert_project_idle(str(project))
                # Another project is independent.
                other = Path(td) / "different"
                self.assertEqual(self._child_attempt(other).returncode, 0)
            self.assertEqual(self._child_attempt(project).returncode, 0)

    def test_sync_reservation_excludes_second_process_and_releases_on_error(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "archive"
            with patch.object(jobs, "_JOBS", {}), \
                 patch.object(jobs, "_RESERVED_PROJECT_WRITERS", {}):
                with self.assertRaisesRegex(ValueError, "injected"):
                    with jobs.reserve_project_output_writer(str(project)):
                        self.assertEqual(self._child_attempt(project).returncode, 7)
                        raise ValueError("injected")
                self.assertEqual(self._child_attempt(project).returncode, 0)

    def test_async_job_fails_without_running_when_other_instance_owns_project(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "archive"
            modified = Path(td) / "should-not-exist"
            with patch.object(jobs, "_JOBS", {}), \
                 patch.object(jobs, "_RESERVED_PROJECT_WRITERS", {}), \
                 patch.object(jobs, "_persist_locked"):
                with lock_project_writer(str(project)):
                    job = jobs.create_job(
                        "archive-bundle-export",
                        lambda: modified.write_text("unsafe", encoding="utf-8"),
                        project_root=str(project),
                    )
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        current = jobs.get_job(job.id)
                        if current["state"] in {"failed", "succeeded"}:
                            break
                        time.sleep(0.02)
                    self.assertEqual(jobs.get_job(job.id)["state"], "failed")
                    self.assertFalse(modified.exists())
            self.assertFalse(modified.exists())


if __name__ == "__main__":
    unittest.main()
