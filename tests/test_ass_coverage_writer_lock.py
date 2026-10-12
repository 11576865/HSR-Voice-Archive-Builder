"""Operating-system cross-process exclusion for ASS coverage writers."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.ass_coverage_lock import LOCK_NAME, exclusive_coverage_writer
from app.ass_coverage_batches import inspect_next_exported_ass_batch


_CHILD = """
import sys
from pathlib import Path
from app.ass_coverage_lock import exclusive_coverage_writer
try:
    with exclusive_coverage_writer(Path(sys.argv[1])):
        print("ACQUIRED")
except RuntimeError:
    print("BUSY")
"""


class CoverageWriterLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def _child(self):
        return subprocess.run(
            [sys.executable, "-c", _CHILD, str(self.folder)],
            cwd=str(Path(__file__).resolve().parents[1]),
            text=True, capture_output=True, timeout=12, check=True,
        )

    def test_two_independent_processes_cannot_own_one_coverage_output(self):
        with exclusive_coverage_writer(self.folder):
            with self.assertRaisesRegex(RuntimeError, "another process"):
                with exclusive_coverage_writer(self.folder):
                    pass
            concurrent = self._child()
            self.assertEqual(concurrent.stdout.strip(), "BUSY")
        after_release = self._child()
        self.assertEqual(after_release.stdout.strip(), "ACQUIRED")
        self.assertTrue((self.folder / LOCK_NAME).is_file())

    def test_previously_canceled_request_does_not_acquire_lock(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(InterruptedError):
            with exclusive_coverage_writer(self.folder, cancel_event=event):
                self.fail("Canceled job unexpectedly acquired the lock")
        self.assertFalse((self.folder / LOCK_NAME).exists())

    def test_public_window_batch_api_wraps_entire_callback_in_lock(self):
        def body(*args, **kwargs):
            with self.assertRaisesRegex(RuntimeError, "another process"):
                with exclusive_coverage_writer(self.folder):
                    pass
            return {"written": True}
        with patch(
            "app.ass_coverage_batches._inspect_next_exported_ass_batch_locked",
            side_effect=body,
        ) as delegate:
            result = inspect_next_exported_ass_batch(
                object(), self.folder, expected_cursor=0,
            )
        self.assertEqual(result, {"written": True})
        self.assertEqual(delegate.call_count, 1)

    def test_lock_file_symlink_does_not_redirect_lock_to_other_output(self):
        target = self.folder / "wrong-target"
        target.write_text("dont-lock-me", encoding="utf-8")
        path = self.folder / LOCK_NAME
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Local filesystem does not support creating symlinks")
        with self.assertRaisesRegex(ValueError, "symlink"):
            with exclusive_coverage_writer(self.folder):
                pass
        self.assertEqual(target.read_text(encoding="utf-8"), "dont-lock-me")


if __name__ == "__main__":
    unittest.main()
