"""Real subprocess regression of the shared jobs.json writer / live-owner recovery."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import jobs
from app.job_journal import recover_snapshot, read_rows, _journal_lock


ROOT = Path(__file__).resolve().parents[1]
CHILD = """
import sys, time
from pathlib import Path
from app import jobs

journal, control, project = map(Path, sys.argv[1:4])
jobs.JOBS_FILE = journal
jobs._load_previous()
def hold():
    control.with_suffix(".started").write_text("running")
    deadline = time.monotonic() + 25
    while not control.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return {"finished": bool(control.exists())}
job = jobs.create_job("archive-verify", hold, project_root=str(project))
print(job.id, flush=True)
deadline = time.monotonic() + 25
while time.monotonic() < deadline:
    state = jobs.get_job(job.id)
    if state and state["state"] in {"succeeded", "failed"}:
        print(state["state"], flush=True)
        break
    time.sleep(0.02)
"""

INSPECT = """
import json, sys
from pathlib import Path
from app import jobs
jobs.JOBS_FILE = Path(sys.argv[1])
jobs._load_previous()
print(json.dumps({j["id"]:j["state"] for j in jobs.recent_jobs(100)}))
"""


REPLAYER = """
import sys, time
from pathlib import Path
from app import jobs
journal, ready, trigger, project = map(Path, sys.argv[1:5])
jobs.JOBS_FILE = journal
jobs._load_previous()
job = jobs.create_job("archive-verify", lambda: "finished", project_root=str(project))
deadline = time.monotonic() + 20
while time.monotonic() < deadline and jobs.get_job(job.id)["state"] != "succeeded":
    time.sleep(0.02)
print(job.id, flush=True)
ready.touch()
while time.monotonic() < deadline and not trigger.exists():
    time.sleep(0.02)
if not trigger.exists():
    raise SystemExit("replay trigger not received")
# Simulate a service persisting an old in-memory completed job without
# first fetching a new snapshot from the shared history.
with jobs._LOCK:
    jobs._persist_locked()
print("replayed", flush=True)
"""


def _rows(path: Path):
    # Honor the same mutex as application writers; Windows atomic replacement
    # can fail if a test holds jobs.json open outside that resource boundary.
    with _journal_lock(path):
        return {row["id"]: row for row in read_rows(path, strict=True)}


class SharedJobJournalProcessTests(unittest.TestCase):
    def _wait(self, condition, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail("Timeout waiting for child job state")

    def _start(self, journal: Path, release: Path, project: Path):
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", CHILD,
             str(journal), str(release), str(project)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self._end_child, proc)
        job_id = proc.stdout.readline().strip()
        self.assertEqual(len(job_id), 32, f"Child did not announce job: {job_id!r}")
        self._wait(lambda: release.with_suffix(".started").exists())
        return proc, job_id

    @staticmethod
    def _end_child(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=4)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

    def test_two_servers_merge_jobs_and_do_not_interrupt_live_owner(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = root / "jobs.json"
            release_a, release_b = root / "release-a", root / "release-b"
            first, id_a = self._start(journal, release_a, root / "first")
            second, id_b = self._start(journal, release_b, root / "second")
            self._wait(lambda: len(_rows(journal)) == 2)
            self.assertEqual(_rows(journal)[id_a]["state"], "running",
                             f"first={first.poll()} second={second.poll()}")
            self.assertEqual(_rows(journal)[id_b]["state"], "running")
            probe = subprocess.run(
                [sys.executable, "-c", INSPECT, str(journal)],
                cwd=ROOT, text=True, capture_output=True, timeout=15, check=False,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            visible = json.loads(probe.stdout)
            self.assertEqual(visible[id_a], "running")
            self.assertEqual(visible[id_b], "running")
            release_a.touch()
            self._wait(lambda: _rows(journal)[id_a]["state"] == "succeeded")
            self.assertEqual(_rows(journal)[id_b]["state"], "running")
            release_b.touch()
            self._wait(lambda: _rows(journal)[id_b]["state"] == "succeeded")
            self.assertEqual(set(_rows(journal)), {id_a, id_b})
            self.assertEqual(first.wait(timeout=10), 0)
            self.assertEqual(second.wait(timeout=10), 0)

    def test_dead_owner_is_interrupted_without_changing_live_other_process(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = root / "jobs.json"
            release_a, release_b = root / "release-a", root / "release-b"
            first, id_a = self._start(journal, release_a, root / "first")
            second, id_b = self._start(journal, release_b, root / "second")
            self._wait(lambda: len(_rows(journal)) == 2)
            first.terminate()
            first.wait(timeout=8)
            probe = subprocess.run(
                [sys.executable, "-c", INSPECT, str(journal)],
                cwd=ROOT, text=True, capture_output=True, timeout=15, check=False,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            visible = json.loads(probe.stdout)
            self.assertEqual(visible[id_a], "interrupted")
            self.assertEqual(visible[id_b], "running")
            self.assertEqual(_rows(journal)[id_b]["state"], "running")
            release_b.touch()
            self._wait(lambda: _rows(journal)[id_b]["state"] == "succeeded")
            self.assertEqual(second.wait(timeout=10), 0)


    def test_tombstone_prevents_stale_live_owner_from_republishing_deleted_job(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal, ready, trigger = root / "jobs.json", root / "ready", root / "replay"
            project = root / "archive"
            actor = subprocess.Popen(
                [sys.executable, "-u", "-c", REPLAYER,
                 str(journal), str(ready), str(trigger), str(project)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.addCleanup(self._end_child, actor)
            old_id = actor.stdout.readline().strip()
            self.assertEqual(len(old_id), 32)
            self._wait(ready.exists)
            with patch.object(jobs, "JOBS_FILE", journal), \
                 patch.object(jobs, "_JOBS", {}), \
                 patch.object(jobs, "_DELETED_JOB_IDS", set()):
                self.assertEqual(jobs.delete_project_jobs(str(project)), 1)
                self.assertNotIn(old_id, _rows(journal))
                trigger.touch()
                self.assertEqual(actor.stdout.readline().strip(), "replayed")
                self.assertEqual(actor.wait(timeout=8), 0)
                self.assertNotIn(old_id, _rows(journal))
                self.assertIsNone(jobs.get_job(old_id))
                self.assertEqual(jobs.delete_project_jobs(str(project)), 0)
                tombstones = json.loads((root / "jobs.json.deleted.json").read_text("utf-8"))
                self.assertIn(old_id, tombstones["job_ids"])

    def test_existing_server_recovers_dead_owner_during_status_poll_without_reboot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = root / "jobs.json"
            release = root / "release"
            actor, old_id = self._start(journal, release, root / "archive")
            with patch.object(jobs, "JOBS_FILE", journal), \
                 patch.object(jobs, "_JOBS", {}), \
                 patch.object(jobs, "_DELETED_JOB_IDS", set()):
                self.assertEqual(jobs.get_job(old_id)["state"], "running")
                actor.terminate()
                actor.wait(timeout=8)
                self._wait(lambda: jobs.get_job(old_id)["state"] == "interrupted")
                self.assertEqual(_rows(journal)[old_id]["state"], "interrupted")

    def test_deletion_tombstone_survives_failed_json_publication(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = root / "jobs.json"
            project = root / "archive"
            old_id = "a" * 32
            journal.write_text(json.dumps([{
                "id": old_id, "kind": "archive-verify", "state": "succeeded",
                "message": "done", "project_root": str(project),
                "created_at": "2026-10-01T00:00:00Z",
            }]), encoding="utf-8")
            from app import job_journal
            original_write = job_journal.atomic_write_json
            def injected_failure(path, payload):
                if Path(path) == journal:
                    raise OSError("injected disk failure after tombstone")
                return original_write(path, payload)
            with patch.object(jobs, "JOBS_FILE", journal), \
                 patch.object(jobs, "_JOBS", {}), \
                 patch.object(jobs, "_DELETED_JOB_IDS", set()):
                with patch.object(job_journal, "atomic_write_json", side_effect=injected_failure):
                    with self.assertRaisesRegex(OSError, "injected disk failure"):
                        jobs.delete_project_jobs(str(project))
                # A failed write left the old jobs.json row in place, but the
                # durable tombstone is already authoritative on next recovery.
                self.assertIn(old_id, _rows(journal))
                visible, deleted = recover_snapshot(journal, limit=100)
                self.assertIn(old_id, deleted)
                self.assertNotIn(old_id, {row["id"] for row in visible})
                self.assertIsNone(jobs.get_job(old_id))

    def test_malformed_deletion_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = root / "jobs.json"
            sidecar = root / "jobs.json.deleted.json"
            journal.write_text("[]", encoding="utf-8")
            sidecar.write_text("{broken", encoding="utf-8")
            with patch.object(jobs, "JOBS_FILE", journal), \
                 patch.object(jobs, "_JOBS", {}), \
                 patch.object(jobs, "_DELETED_JOB_IDS", set()):
                with self.assertRaisesRegex(RuntimeError, "deletion ledger is unreadable"):
                    jobs.create_job("archive-verify", lambda: None, project_root=str(root))
                self.assertEqual(journal.read_text("utf-8"), "[]")
                self.assertEqual(sidecar.read_text("utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
