"""Changing layout parameters is not an ASS rendering or MKV mux command."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.archive_bundle_lifecycle import (
    OUTPUT, bundle_export_status, record_bundle_publication,
)
from app.project import ProjectConfig, update_project
from app.subtitle_export_status import (
    EXPORT_RECEIPT_NAME, read_subtitle_export_health, record_subtitle_export,
)


class LayoutAutosaveDoesNotExportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.out = self.root / "output"
        self.out.mkdir()
        (self.out / "manifest.json").write_text(
            '{"entries":[{"index":1,"start_seconds":1.0,"audio_end_seconds":2.0}]}',
            encoding="utf-8",
        )
        (self.out / "HSR_Voice_Archive.ass").write_text(
            "Style: Primary,X\nDialogue: 0,0:00:01.00,0:00:02.00,Primary,,0,0,0,,OLD ASS",
            encoding="utf-8",
        )
        (self.out / "HSR_Voice_Archive.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nOLD SRT\n",
            encoding="utf-8",
        )
        (self.out / "continuous.flac").write_bytes(b"old-flac-source")
        record_subtitle_export(self.out, state="current", ass_required=True)
        (self.out / OUTPUT).write_bytes(b"old-verified-mkv")
        record_bundle_publication(
            self.out, background_color="#000000",
            background_image=None, image_extension=None,
            include_fonts=False,
        )
        self.config = ProjectConfig(
            schema_version=1, name="test", root=str(self.root),
            index_csv="index.csv", wav_source="wav",
            output_dir="output", delivery_mode="complete",
        )

    def _source_bytes(self):
        return {
            name: (self.out / name).read_bytes()
            for name in (
                "HSR_Voice_Archive.ass", "HSR_Voice_Archive.srt",
                "continuous.flac", OUTPUT, "manifest.json",
            )
        }

    def test_changed_layout_is_saved_without_rewriting_any_artifact(self):
        before = self._source_bytes()
        self.assertEqual(bundle_export_status(self.out)["state"], "current")
        with patch("app.project.save_project"):
            update_project(self.config, subtitle_chs_size=55)
        self.assertEqual(self.config.subtitle_chs_size, 55)
        self.assertEqual(self._source_bytes(), before)
        status = read_subtitle_export_health(self.out)
        self.assertEqual(status["state"], "stale")
        self.assertTrue(status["srt_current"])
        self.assertFalse(status["ass_current"])
        self.assertFalse(status["artifacts_current"])
        self.assertIn("生成", status["artifact_error"])
        self.assertEqual(bundle_export_status(self.out)["state"], "stale")

    def test_noop_save_never_invalidates_current_artifacts(self):
        with patch("app.project.save_project"):
            update_project(self.config, subtitle_chs_size=48)
        self.assertEqual(read_subtitle_export_health(self.out)["state"], "current")
        self.assertEqual(bundle_export_status(self.out)["state"], "current")
        receipt_before = (self.out / EXPORT_RECEIPT_NAME).read_bytes()
        with patch("app.project.save_project"):
            update_project(self.config, name="unrelated-rename")
        self.assertEqual((self.out / EXPORT_RECEIPT_NAME).read_bytes(), receipt_before)

    def test_explicit_successful_export_clears_style_stale_marker(self):
        with patch("app.project.save_project"):
            update_project(self.config, subtitle_primary_font="Updated Font")
        self.assertEqual(read_subtitle_export_health(self.out)["state"], "stale")
        record_subtitle_export(self.out, state="current", ass_required=True)
        self.assertEqual(read_subtitle_export_health(self.out)["state"], "current")
        self.assertNotIn(
            "ass_style_stale", {k for k, v in json.loads(
                (self.out / EXPORT_RECEIPT_NAME).read_text(encoding="utf-8")
            ).items() if v is True and k != "ass_required"},
        )

    def test_autosave_frontend_has_no_hidden_export_scheduler(self):
        html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text(
            encoding="utf-8",
        )
        # No automatic generation after any save or settings change.
        self.assertNotIn("queueAutoAssRefresh", html)
        self.assertNotIn("runAutoAssRefresh", html)
        body = html.split("async function saveSubtitleLayoutSettings(", 1)[1].split(
            "// Shared immutable *complete* subtitle snapshot", 1
        )[0]
        self.assertNotIn("/subtitles/artifacts/refresh", body)
        self.assertNotIn("startAssExport()", body)
        self.assertIn("queueSubtitleSettingsAutosave", html)
        self.assertIn(
            "if(await saveSubtitleLayoutSettings())await startAssExport('internal');",
            html, "Explicit Generate must continue to save latest parameters first",
        )
        self.assertIn("function queueLiveRealPreview()", html)
        self.assertIn("merely from settings changes", html)


if __name__ == "__main__":
    unittest.main()
