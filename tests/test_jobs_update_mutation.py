"""Project mutation scheduling guard for HSR archive rebuilds and remote apply."""
from __future__ import annotations

import threading
import time
import unittest

from app.jobs import create_job, get_job


class ArchiveJobMutationBoundaryTests(unittest.TestCase):
    def test_build_and_remote_apply_cannot_modify_same_project_concurrently(self):
        waiting = threading.Event()
        started = threading.Event()
        project_root = "/test-hsr-mutating-unique-2026-10-11"

        def slow_remote() -> dict[str, bool]:
            started.set()
            if not waiting.wait(timeout=8):
                raise RuntimeError("Timed out waiting for test release")
            return {"finished": True}

        job = create_job(
            "remote-update-apply", slow_remote, project_root=project_root,
            project_name="test-isolated",
        )
        try:
            self.assertTrue(started.wait(timeout=5), "remote apply did not start")
            with self.assertRaisesRegex(RuntimeError, "拒绝并发"):
                create_job("build", lambda: None, project_root=project_root)
            with self.assertRaisesRegex(RuntimeError, "拒绝并发"):
                create_job("remote-update-apply", lambda: None, project_root=project_root)
        finally:
            waiting.set()

        for _ in range(100):
            if (get_job(job.id) or {}).get("state") == "succeeded":
                break
            time.sleep(0.03)
        self.assertEqual((get_job(job.id) or {}).get("state"), "succeeded")

        new_job = create_job(
            "remote-update-apply", lambda: {"status": "ok"},
            project_root=project_root,
        )
        for _ in range(100):
            if (get_job(new_job.id) or {}).get("state") == "succeeded":
                break
            time.sleep(0.03)
        self.assertEqual((get_job(new_job.id) or {}).get("state"), "succeeded")


if __name__ == "__main__":
    unittest.main()
