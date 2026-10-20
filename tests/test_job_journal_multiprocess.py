"""Real subprocess regression of the shared jobs.json writer / live-owner recovery."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


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


def _rows(path: Path):
    return {row["id"]: row for row in json.loads(path.read_text(encoding="utf-8"))}


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
            self.assertEqual(_rows(journal)[id_a]["state"], "running")
            self.assertEqual(_rows(journal)[id_b]["state"], "running")
            probe = subprocess.run(
                [sys.executable, "-c", INSPECT, str(journal)],
                cwd=ROOT, text=True, capture_output=True, timeout=15, check=True,
            )
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
                cwd=ROOT, text=True, capture_output=True, timeout=15, check=True,
            )
            visible = json.loads(probe.stdout)
            self.assertEqual(visible[id_a], "interrupted")
            self.assertEqual(visible[id_b], "running")
            self.assertEqual(_rows(journal)[id_b]["state"], "running")
            release_b.touch()
            self._wait(lambda: _rows(journal)[id_b]["state"] == "succeeded")
            self.assertEqual(second.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
