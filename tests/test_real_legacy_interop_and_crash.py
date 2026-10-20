"""Cross-version and kill-injection verification of the *actual* #198 HSR code.

The legacy checkout is pinned in CI, not an imitation of its JSON parser.
All journal files and projects are disposable test data.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import jobs
from app.job_journal import (
    SESSION_ID, _tombstone_path, compact_tombstones, persist_rows,
    recover_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
LEGACY_COMMIT = "f366f3fc6da47b8c06bb961945d4658da21d6a98"
LEGACY = os.environ.get("HSR_LEGACY_CHECKOUT", "")

OLD_ACTOR = r"""
import sys, time
from pathlib import Path
from app import jobs
from app.job_journal import _read_deleted

mode, journal_str, project_str, ready_str, release_str = sys.argv[1:]
journal, project, ready, release = map(Path, (journal_str, project_str, ready_str, release_str))
jobs.JOBS_FILE = journal
jobs._load_previous()
if mode == "producer":
    job = jobs.create_job("archive-verify", lambda: "complete", project_root=str(project))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and jobs.get_job(job.id)["state"] != "succeeded":
        time.sleep(0.02)
    if jobs.get_job(job.id)["state"] != "succeeded":
        raise SystemExit("old producer job did not finish")
    print(job.id, flush=True)
elif mode == "deleter":
    removed = jobs.delete_project_jobs(str(project))
    print("deleted=" + str(removed), flush=True)
else:
    raise SystemExit("unknown actor mode")
ready.touch()
deadline = time.monotonic() + 20
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
if not release.exists():
    raise SystemExit("release timed out")
# #198's authentic parser checks schema_version == 1, ignoring optional
# metadata. This is a source-derived interoperability assertion.
print("readable=" + str(len(_read_deleted(journal))), flush=True)
with jobs._LOCK:
    jobs._persist_locked()
print("flushed", flush=True)
"""

KILLED_ACTOR = r"""
import os, sys
from pathlib import Path
from app import job_journal, jobs
from app.job_journal import _tombstone_path, compact_tombstones

phase, journal_s, project_s, ready_s = sys.argv[1:]
journal, project, ready = map(Path, (journal_s, project_s, ready_s))
jobs.JOBS_FILE = journal
if phase == "delete":
    jobs._load_previous()
    original = job_journal.atomic_write_json
    def intercepted(path, payload):
        if Path(path) == journal:
            # The tombstone is already durable; the raw old row remains.
            ready.touch()
            os._exit(71)
        return original(path, payload)
    job_journal.atomic_write_json = intercepted
    jobs.delete_project_jobs(str(project))
elif phase == "compact":
    original = job_journal.atomic_write_json
    def intercepted(path, payload):
        if Path(path) == _tombstone_path(journal):
            # The raw journal was cleared but the old tombstone remains.
            ready.touch()
            os._exit(72)
        return original(path, payload)
    job_journal.atomic_write_json = intercepted
    compact_tombstones(journal, limit=100)
else:
    raise SystemExit("unknown crash mode")
"""


def _raw_rows(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    return {x["id"]: x for x in json.loads(path.read_text("utf-8"))}


class RealLegacyInteropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not LEGACY:
            raise unittest.SkipTest("Requires pinned #198 checkout via HSR_LEGACY_CHECKOUT")
        legacy = Path(LEGACY).resolve()
        if not (legacy / "app" / "jobs.py").is_file():
            raise AssertionError(f"Missing real legacy HSR checkout: {legacy}")
        actual = subprocess.run(
            ["git", "-C", str(legacy), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True, timeout=10,
        ).stdout.strip()
        if actual != LEGACY_COMMIT:
            raise AssertionError(f"Unexpected legacy code identity: {actual}")
        cls.legacy = legacy

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.journal = self.base / "jobs.json"
        self.project = self.base / "archive"
        self.ready = self.base / "actor.ready"
        self.release = self.base / "actor.release"

    def _launch(self, script: str, mode: str, *, legacy: bool = False):
        checkout = self.legacy if legacy else ROOT
        env = os.environ.copy()
        env["PYTHONPATH"] = str(checkout)
        p = subprocess.Popen(
            [sys.executable, "-u", "-c", script, mode,
             str(self.journal), str(self.project), str(self.ready),
             *( [str(self.release)] if script == OLD_ACTOR else [] )],
            cwd=checkout, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self._stop, p)
        return p

    @staticmethod
    def _stop(p):
        if p.poll() is None:
            p.kill()
            p.wait(timeout=6)
        p.stdout.close()
        p.stderr.close()

    def _await_ready(self):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.ready.is_file():
                return
            time.sleep(0.02)
        self.fail("Subprocess did not reach transition boundary")

    def test_actual_v198_producer_and_v200_deletion_compaction(self):
        old = self._launch(OLD_ACTOR, "producer", legacy=True)
        job_id = old.stdout.readline().strip()
        self.assertEqual(len(job_id), 32, f"Old producer: {job_id}; {old.poll()}")
        self._await_ready()
        with patch.object(jobs, "JOBS_FILE", self.journal), \
             patch.object(jobs, "_JOBS", {}), \
             patch.object(jobs, "_DELETED_JOB_IDS", set()):
            self.assertEqual(jobs.delete_project_jobs(str(self.project)), 1)
            ledger = json.loads(_tombstone_path(self.journal).read_text("utf-8"))
            self.assertEqual(ledger["schema_version"], 1)
            self.assertEqual(ledger["job_ids"], [job_id])
            self.assertEqual(jobs.compact_job_history_tombstones(),
                             {"removed": 0, "retained": 1})
            self.release.touch()
            self.assertEqual(old.stdout.readline().strip(), "readable=1")
            self.assertEqual(old.stdout.readline().strip(), "flushed")
            self.assertEqual(old.wait(timeout=7), 0)
            self.assertNotIn(job_id, _raw_rows(self.journal))
            self.assertEqual(jobs.compact_job_history_tombstones(),
                             {"removed": 1, "retained": 0})
            self.assertIsNone(jobs.get_job(job_id))

    def test_actual_v198_deleter_can_reassert_tombstone_after_v200_compaction(self):
        # Genuine known limitation: #198 retains its local deleted IDs after
        # successful journal write, and can recreate a pruned sidecar entry.
        # The *job* must not resurrect; compaction's space bound cannot be
        # promised across concurrently active legacy deleting processes.
        job_id = "a" * 32
        self.journal.write_text(json.dumps([{
            "id": job_id, "kind": "archive-verify", "state": "succeeded",
            "message": "completed", "created_at": "2026-10-01T00:00:00Z",
            "project_root": str(self.project), "owner_session": "f" * 32,
        }]), encoding="utf-8")
        old = self._launch(OLD_ACTOR, "deleter", legacy=True)
        self.assertEqual(old.stdout.readline().strip(), "deleted=1")
        self._await_ready()
        self.assertNotIn(job_id, _raw_rows(self.journal))
        # #198's deletion mark has no owner proof, so the new compactor
        # must refuse to prune rather than guessing the producer's death.
        self.assertEqual(compact_tombstones(self.journal, limit=100),
                         {"removed": 0, "retained": 1})
        self.release.touch()
        self.assertEqual(old.stdout.readline().strip(), "readable=1")
        self.assertEqual(old.stdout.readline().strip(), "flushed")
        self.assertEqual(old.wait(timeout=7), 0)
        self.assertNotIn(job_id, _raw_rows(self.journal))
        self.assertEqual(compact_tombstones(self.journal, limit=100),
                         {"removed": 0, "retained": 1})


class RealProcessCrashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.journal = self.base / "jobs.json"
        self.project = self.base / "archive"
        self.ready = self.base / "checkpoint"
        self.record = {
            "id": "a" * 32, "kind": "archive-verify", "state": "succeeded",
            "message": "complete", "created_at": "2026-10-01T00:00:00Z",
            "project_root": str(self.project), "owner_session": "f" * 32,
        }

    def _run_crash(self, mode):
        p = subprocess.Popen(
            [sys.executable, "-u", "-c", KILLED_ACTOR, mode,
             str(self.journal), str(self.project), str(self.ready)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and p.poll() is None:
                time.sleep(0.02)
            self.assertEqual(p.wait(timeout=3), 71 if mode == "delete" else 72,
                             p.stderr.read())
            self.assertTrue(self.ready.is_file(), "Child never hit fault boundary")
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=4)
            p.stdout.close()
            p.stderr.close()

    def test_process_exits_after_tombstone_before_journal_rewrite(self):
        self.journal.write_text(json.dumps([self.record]), encoding="utf-8")
        self._run_crash("delete")
        self.assertIn(self.record["id"], _raw_rows(self.journal))
        ids = json.loads(_tombstone_path(self.journal).read_text("utf-8"))["job_ids"]
        self.assertIn(self.record["id"], ids)
        visible, _ = recover_snapshot(self.journal, limit=100)
        self.assertEqual(visible, [])
        self.assertEqual(_raw_rows(self.journal), {})
        self.assertEqual(compact_tombstones(self.journal, limit=100),
                         {"removed": 1, "retained": 0})

    def test_process_exits_after_journal_rewrite_before_tombstone_reduction(self):
        self.journal.write_text(json.dumps([self.record]), encoding="utf-8")
        persist_rows(self.journal, [], limit=100, deleted={self.record["id"]})
        # Force a stale raw row like an old process might have done before
        # a coordinated recovery; compaction removes the row first.
        self.journal.write_text(json.dumps([self.record]), encoding="utf-8")
        self._run_crash("compact")
        self.assertEqual(_raw_rows(self.journal), {})
        self.assertIn(
            self.record["id"],
            json.loads(_tombstone_path(self.journal).read_text("utf-8"))["job_ids"],
        )
        self.assertEqual(compact_tombstones(self.journal, limit=100),
                         {"removed": 1, "retained": 0})


if __name__ == "__main__":
    unittest.main()
