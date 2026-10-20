"""Sustained concurrent task progress and abrupt-owner-death regressions.

Uses disposable state, real child interpreters, real journal/OS locks, and
long-running in-process work callbacks (not FFmpeg or mobile hardware).
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

from app import jobs
from app.job_journal import recover_snapshot

ROOT = Path(__file__).resolve().parents[1]
STEPS = 180
CHILD = r"""
import sys, time
from pathlib import Path
from app import jobs

journal, project, steps_s = sys.argv[1:]
jobs.JOBS_FILE = Path(journal)
steps = int(steps_s)

def run(report):
    for index in range(1, steps + 1):
        report("processing", "sustained-work", index, steps)
        time.sleep(0.018)
    return {"steps": steps}

task = jobs.create_job("archive-verify", run, with_progress=True,
                       project_root=project)
print(task.id, flush=True)
deadline = time.monotonic() + 32
while time.monotonic() < deadline:
    result = jobs.get_job(task.id)
    if result is not None and result["state"] in {"succeeded", "failed"}:
        print(result["state"], flush=True)
        break
    time.sleep(0.03)
else:
    raise SystemExit("task did not finish")
"""

OBSERVER = r"""
import sys, time
from pathlib import Path
from app import jobs
journal, first, second = sys.argv[1:]
jobs.JOBS_FILE = Path(journal)
observations = 0
last = {first: 0, second: 0}
terminal = {}
deadline = time.monotonic() + 32
while time.monotonic() < deadline:
    for job_id in (first, second):
        row = jobs.get_job(job_id)
        if row is None:
            raise SystemExit("missing job: " + job_id)
        if row["progress_current"] < last[job_id]:
            raise SystemExit("progress regressed for " + job_id)
        last[job_id] = row["progress_current"]
        if row["state"] in ("failed", "interrupted"):
            raise SystemExit("unexpected live task failure: " + row["state"])
        if row["state"] == "succeeded":
            terminal[job_id] = row["state"]
    observations += 1
    if len(terminal) == 2:
        break
    time.sleep(0.015)
if len(terminal) != 2:
    raise SystemExit("observer timed out: " + repr(last))
print("observed=%d %d %d" % (observations, last[first], last[second]))
"""


class SustainedJobJournalProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.journal = root / "jobs.json"
        self.projects = [root / "a", root / "b"]

    @staticmethod
    def _stop(proc):
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

    def _worker(self, project):
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", CHILD,
             str(self.journal), str(project), str(STEPS)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self._stop, proc)
        job_id = proc.stdout.readline().strip()
        self.assertEqual(len(job_id), 32, "Task failed to announce: " + repr(job_id))
        return proc, job_id

    def _wait_for(self, check, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.02)
        self.fail("Timed out waiting for concurrent job progress")

    def _states(self):
        rows, _deleted = recover_snapshot(self.journal, limit=100)
        return {row["id"]: row for row in rows}

    def test_two_sustained_producers_and_independent_observers(self):
        a, id_a = self._worker(self.projects[0])
        b, id_b = self._worker(self.projects[1])
        observers = []
        for _ in range(2):
            p = subprocess.Popen(
                [sys.executable, "-u", "-c", OBSERVER,
                 str(self.journal), id_a, id_b],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(self._stop, p)
            observers.append(p)
        for observer in observers:
            stdout, stderr = observer.communicate(timeout=36)
            self.assertEqual(observer.returncode, 0, stderr)
            self.assertIn("observed=", stdout)
            self.assertIn(f" {STEPS} {STEPS}", stdout)
        for worker in (a, b):
            stdout, stderr = worker.communicate(timeout=12)
            self.assertEqual(worker.returncode, 0, stderr)
            self.assertIn("succeeded", stdout)
        last = self._states()
        self.assertEqual(set(last), {id_a, id_b})
        for job_id in (id_a, id_b):
            self.assertEqual(last[job_id]["state"], "succeeded")
            self.assertEqual(last[job_id]["progress_current"], STEPS)
            self.assertEqual(last[job_id]["progress_total"], STEPS)

    def test_killed_progress_owner_is_interrupted_but_peer_finishes(self):
        a, id_a = self._worker(self.projects[0])
        b, id_b = self._worker(self.projects[1])
        self._wait_for(lambda: self._states().get(id_a, {}).get("progress_current", 0) >= 32)
        live = self._states()
        self.assertEqual(live[id_a]["state"], "running")
        self.assertEqual(live[id_b]["state"], "running")
        a.kill()
        a.wait(timeout=8)
        self._wait_for(lambda: self._states().get(id_a, {}).get("state") == "interrupted")
        self.assertNotIn(self._states()[id_b]["state"], {"interrupted", "failed"})
        stdout, stderr = b.communicate(timeout=35)
        self.assertEqual(b.returncode, 0, stderr)
        self.assertIn("succeeded", stdout)
        end = self._states()
        self.assertEqual(end[id_a]["state"], "interrupted")
        self.assertGreater(end[id_a]["progress_current"], 0)
        self.assertLess(end[id_a]["progress_current"], STEPS)
        self.assertEqual(end[id_b]["state"], "succeeded")
        self.assertEqual(end[id_b]["progress_current"], STEPS)
        # A brand-new service must keep these outcomes after startup recovery.
        script = r"""
import json, sys
from pathlib import Path
from app import jobs
jobs.JOBS_FILE = Path(sys.argv[1])
jobs._load_previous()
print(json.dumps({j["id"]: j["state"] for j in jobs.recent_jobs(100)}))
"""
        probe = subprocess.run(
            [sys.executable, "-c", script, str(self.journal)],
            cwd=ROOT, capture_output=True, text=True, timeout=18,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        states = json.loads(probe.stdout)
        self.assertEqual(states[id_a], "interrupted")
        self.assertEqual(states[id_b], "succeeded")


if __name__ == "__main__":
    unittest.main()
