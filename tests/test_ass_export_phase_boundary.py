"""ASS generation must not repeat FFmpeg/libass raster audits of every cue."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.subtitles import (
    _ass_artifact_render_config,
    refresh_ass_only,
    refresh_subtitle_artifacts_from_settings,
)
from subtitle_layout.ass_writer import render_ass
from subtitle_layout.config import SubtitleRenderConfig


class AssExportPhaseBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = replace(
            SubtitleRenderConfig.plain_text_preset(),
            verify_libass_layout=True,
        )

    def test_fast_export_mode_preserves_settings_without_real_ink_probes(self):
        export_cfg = _ass_artifact_render_config(self.cfg)
        self.assertTrue(self.cfg.verify_libass_layout)
        self.assertFalse(export_cfg.verify_libass_layout)
        entry = SimpleNamespace(
            id=1, english="This is an archive.", chinese="这是档案。",
            start_seconds=1.0, display_end_seconds=3.0,
        )
        with patch("subtitle_layout.ass_writer.validate_real_ink",
                   side_effect=AssertionError("export must not raster-probe")) as oracle:
            ass = render_ass([entry], config=export_cfg)
        oracle.assert_not_called()
        self.assertIn("Dialogue:", ass)

    def test_full_subtitle_artifact_refresh_does_not_raster_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "manifest.json").write_text(
                json.dumps({"entries": []}), encoding="utf-8",
            )
            captured = []
            with (
                patch("app.subtitles.record_subtitle_export"),
                patch("app.subtitles.write_srt"),
                patch("app.subtitles.write_ass",
                      side_effect=lambda *args, **kwargs:
                      captured.append(kwargs["config"])),
                patch("app.subtitles.apply_cached_word_alignments"),
                patch("app.subtitles._sync_corrected_csv"),
            ):
                refreshed = refresh_subtitle_artifacts_from_settings(
                    output, render_config=self.cfg, generate_ass=True,
                )
            self.assertEqual(refreshed["ass_error"], "")
            self.assertEqual(len(captured), 1)
            self.assertFalse(captured[0].verify_libass_layout)
            self.assertTrue(self.cfg.verify_libass_layout)

    def test_ass_only_refresh_does_not_raster_probe_or_rewrite_srt(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "manifest.json").write_text(
                json.dumps({"entries": []}), encoding="utf-8",
            )
            original_srt = b"unchanged SRT"
            (output / "HSR_Voice_Archive.srt").write_bytes(original_srt)
            captured = []
            config = SimpleNamespace(
                source_text_language="en", target_language="zh-CN"
            )
            with (
                patch("app.subtitles.subtitle_render_config", return_value=self.cfg),
                patch("app.subtitles.record_subtitle_export"),
                patch("app.subtitles.write_ass",
                      side_effect=lambda *args, **kwargs:
                      captured.append(kwargs["config"])),
                patch("app.subtitles.apply_cached_word_alignments"),
            ):
                refreshed = refresh_ass_only(config, output)
            self.assertEqual(refreshed["ass_error"], "")
            self.assertEqual(len(captured), 1)
            self.assertFalse(captured[0].verify_libass_layout)
            self.assertEqual(
                (output / "HSR_Voice_Archive.srt").read_bytes(),
                original_srt,
            )


if __name__ == "__main__":
    unittest.main()
