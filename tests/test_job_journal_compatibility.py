"""Shared job journal fails closed on corruption and recovers legacy jobs."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import jobs
from app.job_journal import recover_rows, session_is_alive


class JobJournalCompatibilityTests(unittest.TestCase):
    def test_legacy_live_records_become_interrupted_not_discarded(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.json"
            path.write_text(json.dumps([
                {"id": "1" * 32, "kind": "build", "state": "running",
                 "message": "still working", "created_at": "2026-10-01T00:00:00Z"},
                {"id": "2" * 32, "kind": "archive-verify", "state": "succeeded",
                 "message": "done", "created_at": "2026-10-01T00:00:01Z"},
            ]), encoding="utf-8")
            rows = recover_rows(path, limit=100)
            self.assertEqual(rows[0]["state"], "interrupted")
            self.assertEqual(rows[1]["state"], "succeeded")
            self.assertEqual(len(json.loads(path.read_text("utf-8"))), 2)

    def test_corrupt_journal_is_not_replaced_or_left_as_ghost_queued_job(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.json"
            for invalid in ("{invalid", '[{"not_a_job_id": "x"}]'):
                with self.subTest(invalid=invalid):
                    path.write_text(invalid, encoding="utf-8")
                    with patch.object(jobs, "JOBS_FILE", path), \
                         patch.object(jobs, "_JOBS", {}), \
                         patch.object(jobs, "_DELETED_JOB_IDS", set()):
                        with self.assertRaisesRegex(RuntimeError, "unreadable"):
                            jobs.create_job(
                                "archive-verify", lambda: None,
                                project_root=str(Path(td) / "project"),
                            )
                        self.assertEqual(jobs._JOBS, {})
                    self.assertEqual(path.read_text("utf-8"), invalid)

    def test_untrusted_session_identity_does_not_make_path_lease(self):
        self.assertFalse(session_is_alive("../../../somewhere/else/injected"))
        self.assertFalse(session_is_alive("not-a-valid-uuid-not-hex-content"))


if __name__ == "__main__":
    unittest.main()
