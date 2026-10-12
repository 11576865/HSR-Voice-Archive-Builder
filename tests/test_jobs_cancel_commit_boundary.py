"""Deterministic cancellation/commit ordering for persistent ASS evidence jobs."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.jobs import cancel_job, create_job, get_job


def await_final(job_id: str, timeout: float = 6.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = get_job(job_id)
        if status and status["state"] in {"succeeded", "failed", "canceled"}:
            return status
        time.sleep(.01)
    raise AssertionError("Background evidence job did not finish")


class EvidenceCommitCancellationRaceTests(unittest.TestCase):
    def test_late_cancel_cannot_disguise_committed_evidence_as_canceled(self):
        with tempfile.TemporaryDirectory() as folder:
            root = str(Path(folder).resolve())
            proof = Path(folder) / "ass_render_coverage_checkpoint.json"
            committed = threading.Event()
            return_allowed = threading.Event()

            def save_and_return(cancel_event, progress):
                # Simulate actual durable commit before the callback returns.
                proof.write_text('{"next_cursor": 3}', encoding="utf-8")
                committed.set()
                if not return_allowed.wait(5.0):
                    raise RuntimeError("late-cancel test timed out")
                return {"persisted_checkpoint": str(proof), "next_cursor": 3}

            job = create_job(
                "ass-artifact-evidence", save_and_return,
                with_cancel=True, with_progress=True, project_root=root,
            )
            try:
                self.assertTrue(committed.wait(4.0))
                self.assertEqual(get_job(job.id)["state"], "running")
                request = cancel_job(job.id, project_root=root)
                self.assertTrue(request["cancel_requested"])
                return_allowed.set()
                finished = await_final(job.id)
                self.assertEqual(finished["state"], "succeeded")
                self.assertEqual(finished["result"]["next_cursor"], 3)
                self.assertTrue(finished["cancel_requested"])
                self.assertIn("已提交", finished["message"])
                self.assertTrue(proof.is_file())
                with self.assertRaises(ValueError):
                    cancel_job(job.id, project_root=root)
            finally:
                return_allowed.set()

    def test_cancel_before_persistence_is_canceled_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = str(Path(folder).resolve())
            marker = Path(folder) / "proof.json"
            started = threading.Event()

            def cooperative_worker(cancel_event, progress):
                started.set()
                if not cancel_event.wait(5.0):
                    raise RuntimeError("cancel never arrived")
                raise InterruptedError("canceled before evidence commit")

            job = create_job(
                "ass-artifact-evidence", cooperative_worker,
                with_cancel=True, with_progress=True, project_root=root,
            )
            self.assertTrue(started.wait(4.0))
            cancel_job(job.id, project_root=root)
            finished = await_final(job.id)
            self.assertEqual(finished["state"], "canceled")
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
