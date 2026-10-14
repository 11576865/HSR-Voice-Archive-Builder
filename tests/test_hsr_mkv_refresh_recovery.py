"""Regression contracts for browser refresh during long background-MKV mux."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "app" / "static" / "index.html"
EXPORTER = ROOT / "app" / "archive_bundle_exporter.py"


class MkvRefreshRecoveryTests(unittest.TestCase):
    def test_page_initial_status_reattaches_to_existing_job_without_new_export(self):
        script = HTML.read_text(encoding="utf-8")
        start = script.index("async function refresh(){")
        end = script.index("async function watchJob(id){", start)
        refresh = script[start:end]
        self.assertIn("await api('/api/status')", refresh)
        self.assertIn("ongoing.id!==watchedJobId", refresh)
        self.assertIn("void watchJob(ongoing.id)", refresh)
        self.assertNotIn("startArchiveBundle(", refresh)
        self.assertNotIn("'/api/output/archive-bundle'", refresh)

    def test_only_one_progress_poll_is_attached_per_job_and_stalls_are_visible(self):
        script = HTML.read_text(encoding="utf-8")
        start = script.index("async function watchJob(id){")
        end = script.index("\nfor(const button of document.querySelectorAll('.update-filter'))", start)
        watch = script[start:end]
        self.assertIn("watchedJobId===id", watch)
        self.assertIn("++jobPollGeneration", watch)
        self.assertIn("generation!==jobPollGeneration", watch)
        self.assertIn("clearTimeout(pollTimer)", watch)
        self.assertIn("x.job.state==='running'&&x.job.kind==='archive-bundle-export'", watch)
        self.assertIn("elapsed>90000", watch)
        self.assertIn("watchedJobId=null", watch)

    def test_ffmpeg_progress_pipe_drains_stderr_independently(self):
        source = EXPORTER.read_text(encoding="utf-8")
        self.assertIn('tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errors', source)
        self.assertIn("stderr=errors, text=True", source)
        self.assertIn('"-progress", "pipe:1"', source)
        self.assertIn("errors.seek(0)", source)
        self.assertNotIn("stderr=subprocess.PIPE", source)
        self.assertNotIn("proc.stderr.read()", source)

    def test_cue_time_display_is_bounded_without_overwriting_precise_state(self):
        script = HTML.read_text(encoding="utf-8")
        self.assertIn("function displayCueSeconds(value)", script)
        self.assertIn("n.toFixed(3)", script)
        self.assertIn("input.value=state?displayCueSeconds(value):''", script)
        self.assertIn("startInput.value=displayCueSeconds(timing.start)", script)
        self.assertIn("const desiredStart=reset?state.sourceStart:Number(state.start)", script)


if __name__ == "__main__":
    unittest.main()
