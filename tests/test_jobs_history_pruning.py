"""Bounded in-memory HSR job history under long sequential ASS batch usage."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import app.jobs as jobs


class JobHistoryPruningTests(unittest.TestCase):
    @staticmethod
    def _fake_job(identifier: str, state: str) -> jobs.Job:
        return jobs.Job(
            id=identifier, kind="ass-artifact-evidence", state=state,
            message="test", created_at="2026-10-12T00:00:00+00:00",
            project_root="/tmp/hsr-history-test",
        )

    def test_old_completed_jobs_are_pruned_but_queued_and_running_are_preserved(self):
        entries = {
            "active-queued": self._fake_job("active-queued", "queued"),
            "active-running": self._fake_job("active-running", "running"),
        }
        for i in range(150):
            identifier = f"finished-{i:03d}"
            entries[identifier] = self._fake_job(identifier, "succeeded")
        with (patch.object(jobs, "_JOBS", entries),
              patch.object(jobs, "_CANCEL_EVENTS", {}),
              patch.object(jobs, "atomic_write_json") as atomic):
            with jobs._LOCK:
                jobs._persist_locked()
            self.assertEqual(len(entries), jobs.MAX_PERSISTED_JOBS)
            self.assertIn("active-queued", entries)
            self.assertIn("active-running", entries)
            self.assertIn("finished-149", entries)
            self.assertNotIn("finished-000", entries)
            stored = atomic.call_args.args[1]
            self.assertEqual(len(stored), jobs.MAX_PERSISTED_JOBS)
            self.assertIn("active-queued", {r["id"] for r in stored})
            self.assertIn("active-running", {r["id"] for r in stored})

    def test_long_sequence_of_completed_evidence_jobs_does_not_grow_memory_table(self):
        table = {}
        with (patch.object(jobs, "_JOBS", table),
              patch.object(jobs, "_CANCEL_EVENTS", {}),
              patch.object(jobs, "atomic_write_json")):
            first_id = None
            latest_id = None
            for index in range(jobs.MAX_PERSISTED_JOBS + 9):
                task = jobs.create_job(
                    "ass-artifact-evidence", lambda: {"committed": True},
                    project_root="/tmp/hsr-batch-history-pruning",
                )
                if first_id is None:
                    first_id = task.id
                latest_id = task.id
                cutoff = time.monotonic() + 3
                while time.monotonic() < cutoff:
                    current = jobs.get_job(task.id)
                    if current and current["state"] == "succeeded":
                        break
                    time.sleep(.005)
                else:
                    self.fail("evidence job did not reach success state")
            self.assertEqual(len(table), jobs.MAX_PERSISTED_JOBS)
            self.assertIsNone(jobs.get_job(first_id))
            self.assertEqual(jobs.get_job(latest_id)["state"], "succeeded")

    def test_restarted_old_active_job_is_kept_before_newer_completed_history(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / "jobs.json"
            rows = [asdict(self._fake_job("old-active", "running"))]
            rows += [
                asdict(self._fake_job(f"done-{i:03d}", "succeeded"))
                for i in range(jobs.MAX_PERSISTED_JOBS + 3)
            ]
            journal.write_text(json.dumps(rows), encoding="utf-8")
            store = {}
            with (patch.object(jobs, "_JOBS", store),
                  patch.object(jobs, "_CANCEL_EVENTS", {}),
                  patch.object(jobs, "JOBS_FILE", journal),
                  patch.object(jobs, "atomic_write_json") as persist):
                jobs._load_previous()
                self.assertEqual(len(store), jobs.MAX_PERSISTED_JOBS)
                self.assertIn("old-active", store)
                self.assertEqual(store["old-active"].state, "interrupted")
                payload = persist.call_args.args[1]
                self.assertIn("old-active", {item["id"] for item in payload})

    def test_pruning_never_discards_live_jobs_even_if_table_exceeds_limit(self):
        items = {}
        for i in range(jobs.MAX_PERSISTED_JOBS + 3):
            key = f"live-{i}"
            items[key] = self._fake_job(key, "running")
        with (patch.object(jobs, "_JOBS", items),
              patch.object(jobs, "_CANCEL_EVENTS", {}),
              patch.object(jobs, "atomic_write_json") as atomic):
            with jobs._LOCK:
                jobs._persist_locked()
            self.assertEqual(len(items), jobs.MAX_PERSISTED_JOBS + 3)
            self.assertTrue(all(j.state == "running" for j in items.values()))
            persisted = atomic.call_args.args[1]
            self.assertEqual(len(persisted), jobs.MAX_PERSISTED_JOBS + 3)
            self.assertIn("live-0", {record["id"] for record in persisted})


if __name__ == "__main__":
    unittest.main()
