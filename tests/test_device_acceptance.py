"""Read-only Termux archive acceptance and false-positive protection."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.device_acceptance import _last_build, inspect_project


class DeviceAcceptanceTests(unittest.TestCase):
    def _fixture(self, root: Path, *, state: str = "succeeded") -> tuple[Path, Path]:
        project = root / "evanescia-3"
        project.mkdir()
        (project / ".hsr-voice-project.json").write_text(
            json.dumps({"output_dir": "output", "delivery_mode": "complete"}),
            encoding="utf-8",
        )
        out = project / "output"
        out.mkdir()
        (out / "HSR_Voice_Archive_Bundle.mkv").write_bytes(b"fixture bytes")
        (out / "build_report.json").write_text('{"count_total": 1}', encoding="utf-8")
        journal = root / "jobs.json"
        journal.write_text(
            json.dumps([{
                "id": "job-a", "kind": "quick-build",
                "project_root": str(project), "state": state,
                "created_at": "2026-10-27T01:00:00Z",
                "finished_at": "2026-10-27T01:01:00Z",
                "error": (
                    "Traceback (most recent call last):\n"
                    "/home/private/archive/source.wav\n"
                    "TypeError: 'pair' object is not subscriptable"
                ) if state == "failed" else "",
            }]),
            encoding="utf-8",
        )
        return project, journal

    def _inspect(self, project: Path, journal: Path, *, status="current", streams=None):
        if streams is None:
            streams = [
                {"type": "video", "codec": "h264"},
                {"type": "audio", "codec": "flac"},
                {"type": "subtitle", "codec": "ass"},
                {"type": "subtitle", "codec": "subrip"},
            ]
        with (
            patch("app.device_acceptance._source_revision",
                  return_value={"revision": "27c281aba9bc", "pair_fix_present": True}),
            patch("app.device_acceptance.bundle_export_status",
                  return_value={"state": status}),
            patch("app.device_acceptance._probe_media", return_value=streams),
        ):
            return inspect_project(project, journal=journal)

    def test_passing_build_and_sealed_streams(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project, journal = self._fixture(Path(td))
            before = {
                str(p): (p.stat().st_size, p.stat().st_mtime_ns)
                for p in project.rglob("*") if p.is_file()
            }
            result = self._inspect(project, journal)
            after = {
                str(p): (p.stat().st_size, p.stat().st_mtime_ns)
                for p in project.rglob("*") if p.is_file()
            }
            self.assertEqual(before, after)
            self.assertTrue(result["accepted"])
            self.assertEqual(result["reported_voice_count"], 1)
            self.assertEqual(result["build_job"]["finished_at"], "2026-10-27T01:01:00Z")

    def test_failed_job_does_not_relabel_old_valid_mkv_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project, journal = self._fixture(Path(td), state="failed")
            result = self._inspect(project, journal)
            self.assertFalse(result["accepted"])
            self.assertEqual(result["build_job"]["error_type"], "TypeError")
            self.assertNotIn("/home/private", json.dumps(result))
            self.assertNotIn("source.wav", json.dumps(result))

    def test_receipt_stale_or_missing_subtitle_track_rejects_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project, journal = self._fixture(Path(td))
            self.assertFalse(self._inspect(project, journal, status="stale")["accepted"])
            tracks = [
                {"type": "video", "codec": "h264"},
                {"type": "audio", "codec": "flac"},
                {"type": "subtitle", "codec": "ass"},
            ]
            result = self._inspect(project, journal, streams=tracks)
            self.assertFalse(result["accepted"])
            self.assertFalse(result["streams"]["srt"])

    def test_absent_project_returns_incomplete_read_only_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch("app.device_acceptance._source_revision",
                       return_value={"revision": "known", "pair_fix_present": True}):
                result = inspect_project(Path(td) / "missing",
                                         journal=Path(td) / "jobs.json")
            self.assertFalse(result["project_found"])
            self.assertFalse(result["accepted"])
            self.assertFalse((Path(td) / "missing").exists())

    def test_newer_build_job_takes_precedence_over_old_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project, journal = self._fixture(root)
            old_success = json.loads(journal.read_text())[0]
            newest = dict(old_success, id="job-b", state="failed",
                          created_at="2026-10-27T02:00:00Z",
                          error="RuntimeError: failed",
                          finished_at="2026-10-27T02:01:00Z")
            journal.write_text(json.dumps([old_success, newest]), encoding="utf-8")
            result = self._inspect(project, journal)
            self.assertFalse(result["accepted"])
            self.assertEqual(result["build_job"]["error_type"], "RuntimeError")
            self.assertEqual(result["build_job"]["finished_at"], newest["finished_at"])


if __name__ == "__main__":
    unittest.main()
