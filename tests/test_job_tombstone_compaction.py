"""Regression: tombstone compaction must prove original writer death first.

Exercises crash ordering, legacy upgrade and two real Python interpreter
lifetimes. These tests are deliberately independent of production HSR state.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import job_journal, jobs
from app.job_journal import (
    SESSION_ID, compact_tombstones, persist_rows, recover_snapshot,
    _tombstone_path,
)

ROOT = Path(__file__).resolve().parents[1]

# An independent owner keeps a completed job in RAM and can replay it after
# another HSR instance has deleted its history. This tests the actual risk.
CHILD = """
import sys, time
from pathlib import Path
from app import jobs
journal, ready, release, project = map(Path, sys.argv[1:5])
jobs.JOBS_FILE = journal
job = jobs.create_job("archive-verify", lambda: True, project_root=str(project))
deadline = time.monotonic() + 20
while time.monotonic() < deadline and jobs.get_job(job.id)["state"] != "succeeded":
    time.sleep(0.02)
print(job.id, flush=True)
ready.touch()
while time.monotonic() < deadline and not release.exists():
    time.sleep(0.02)
if not release.exists():
    raise SystemExit("release timeout")
with jobs._LOCK:
    jobs._persist_locked()
print("replayed", flush=True)
"""


class TombstoneCompactionTests(unittest.TestCase):
    def _path(self, root: str) -> Path:
        return Path(root) / "jobs.json"

    def _seed(self, path: Path, owner: str, *, job_id: str = "a" * 32) -> None:
        path.write_text(json.dumps([{
            "id": job_id, "kind": "archive-verify", "state": "succeeded",
            "created_at": "2026-10-01T00:00:00+00:00",
            "owner_session": owner,
        }]), encoding="utf-8")
        persist_rows(path, [], limit=100, deleted={job_id})

    def test_legacy_v1_unknown_owner_cannot_be_compacted(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            path.write_text("[]", encoding="utf-8")
            legacy = {"schema_version": 1, "job_ids": ["a" * 32]}
            _tombstone_path(path).write_text(json.dumps(legacy), encoding="utf-8")
            self.assertEqual(compact_tombstones(path, limit=100),
                             {"removed": 0, "retained": 1})
            self.assertEqual(json.loads(_tombstone_path(path).read_text("utf-8")), legacy)
            # New v2 deletion must retain the legacy unknown owner.
            other = "b" * 32
            path.write_text(json.dumps([{
                "id": other, "state": "succeeded", "created_at": "2026-10-01",
                "owner_session": SESSION_ID,
            }]), encoding="utf-8")
            persist_rows(path, [], limit=100, deleted={other})
            record = json.loads(_tombstone_path(path).read_text("utf-8"))
            self.assertEqual(record["schema_version"], 2)
            self.assertEqual(set(record["job_ids"]), {"a" * 32, other})
            self.assertNotIn("a" * 32, record["owner_sessions"])
            self.assertEqual(record["owner_sessions"][other], SESSION_ID)

    def test_live_owner_deletion_does_not_compact(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            self._seed(path, SESSION_ID)
            self.assertEqual(compact_tombstones(path, limit=100),
                             {"removed": 0, "retained": 1})
            self.assertIn("a" * 32,
                          json.loads(_tombstone_path(path).read_text("utf-8"))["job_ids"])

    def test_dead_owner_compaction_removes_sidecar_entry(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            dead_owner = "f" * 32
            self._seed(path, dead_owner)
            self.assertEqual(compact_tombstones(path, limit=100),
                             {"removed": 1, "retained": 0})
            self.assertEqual(json.loads(path.read_text("utf-8")), [])
            record = json.loads(_tombstone_path(path).read_text("utf-8"))
            self.assertEqual(record["job_ids"], [])
            self.assertEqual(record["owner_sessions"], {})
            self.assertEqual(compact_tombstones(path, limit=100),
                             {"removed": 0, "retained": 0})

    def test_sidecar_failure_after_journal_publish_keeps_tombstone(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            self._seed(path, "f" * 32)
            # Simulate crash after deletion-filtered job list is durable but
            # before removal from tombstone sidecar is acknowledged.
            path.write_text(json.dumps([{
                "id": "a" * 32, "kind": "archive-verify", "state": "succeeded",
                "created_at": "2026-10-01", "owner_session": "f" * 32,
            }]), encoding="utf-8")
            original_write = job_journal.atomic_write_json
            def fail_sidecar(path_arg, data):
                if Path(path_arg) == _tombstone_path(path):
                    raise OSError("injected compact failure")
                return original_write(path_arg, data)
            with patch.object(job_journal, "atomic_write_json",
                              side_effect=fail_sidecar):
                with self.assertRaisesRegex(OSError, "injected compact failure"):
                    compact_tombstones(path, limit=100)
            self.assertEqual(json.loads(path.read_text("utf-8")), [])
            old = json.loads(_tombstone_path(path).read_text("utf-8"))
            self.assertIn("a" * 32, old["job_ids"])
            visible, deleted = recover_snapshot(path, limit=100)
            self.assertEqual(visible, [])
            self.assertIn("a" * 32, deleted)
            self.assertEqual(compact_tombstones(path, limit=100)["removed"], 1)

    def test_failed_first_publication_does_not_remove_tombstone(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._path(td)
            self._seed(path, "f" * 32)
            path.write_text(json.dumps([{
                "id": "a" * 32, "kind": "archive-verify", "state": "succeeded",
                "created_at": "2026-10-01", "owner_session": "f" * 32,
            }]), encoding="utf-8")
            with patch.object(job_journal, "atomic_write_json",
                              side_effect=OSError("no journal write")):
                with self.assertRaisesRegex(OSError, "no journal write"):
                    compact_tombstones(path, limit=100)
            self.assertIn("a" * 32, json.loads(_tombstone_path(path).read_text("utf-8"))["job_ids"])
            self.assertEqual(recover_snapshot(path, limit=100)[0], [])

    def test_two_process_owner_liveness_controls_real_compaction(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = self._path(td)
            ready, release, project = root / "ready", root / "release", root / "p"
            proc = subprocess.Popen(
                [sys.executable, "-u", "-c", CHILD,
                 str(path), str(ready), str(release), str(project)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                job_id = proc.stdout.readline().strip()
                self.assertEqual(len(job_id), 32, job_id)
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                with patch.object(jobs, "JOBS_FILE", path), \
                     patch.object(jobs, "_JOBS", {}), \
                     patch.object(jobs, "_DELETED_JOB_IDS", set()):
                    self.assertEqual(jobs.delete_project_jobs(str(project)), 1)
                    self.assertEqual(jobs.compact_job_history_tombstones(),
                                     {"removed": 0, "retained": 1})
                    release.touch()
                    self.assertEqual(proc.stdout.readline().strip(), "replayed")
                    self.assertEqual(proc.wait(timeout=8), 0, proc.stderr.read())
                    self.assertEqual(jobs.compact_job_history_tombstones(),
                                     {"removed": 1, "retained": 0})
                    self.assertIsNone(jobs.get_job(job_id))
                    # Another persist from deleting process cannot
                    # recreate its old tombstones once compaction succeeds.
                    with jobs._LOCK:
                        jobs._persist_locked()
                    self.assertEqual(
                        json.loads(_tombstone_path(path).read_text("utf-8"))["job_ids"], [])
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
