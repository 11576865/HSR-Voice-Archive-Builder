"""Subtitle timing derives exclusively from archive audio positions and gaps."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.subtitle_timing import (
    effective_subtitle_window,
    read_timing_overrides,
    update_subtitle_display_timing,
)
from app.subtitles import (
    get_project_subtitles,
    refresh_subtitle_artifacts_from_settings,
)


class SubtitleTimingOverlayTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.output = Path(self.folder.name)
        self.manifest = self.output / "manifest.json"
        self.original = {
            "entries": [{
                "index": 11,
                "source_member_id": "audio/line-11.wav",
                "start_seconds": 1.25,
                "display_end_seconds": 3.25,
                "audio_end_seconds": 3.00,
                "source_text": "Hello",
                "target_text": "你好",
                "target_text_source": "official_target_lab",
            }, {
                "index": 12,
                "source_member_id": "audio/line-12.wav",
                "start_seconds": 4.00,
                "display_end_seconds": 6.00,
                "source_text": "Next",
                "target_text": "下一条",
            }]
        }
        self.manifest.write_text(
            json.dumps(self.original, ensure_ascii=False), encoding="utf-8"
        )

    def test_timing_edits_are_rejected_without_creating_overrides(self):
        for values in ({"start": 1.10, "end": 3.60}, {"reset": True}):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "音频时序"):
                update_subtitle_display_timing(
                    self.output, item_id=11,
                    expected_start=1.25, expected_end=3.25, **values
                )
        self.assertFalse((self.output / "subtitle_timing_overrides.json").exists())

    def test_legacy_overrides_are_kept_but_ignored_by_ui_ass_and_srt(self):
        legacy = self.output / "subtitle_timing_overrides.json"
        legacy_payload = {"schema_version": 1, "entries": {
            "11": {"start": 0.8, "end": 3.8, "source_start": 1.25,
                   "source_end": 3.25, "source_member_id": "audio/line-11.wav"}
        }}
        legacy.write_text(json.dumps(legacy_payload), encoding="utf-8")
        before = legacy.read_bytes()
        self.assertEqual(effective_subtitle_window(self.original["entries"][0],
                                                    read_timing_overrides(self.output)),
                         (1.25, 3.25))
        config = SimpleNamespace(source_text_language="en", reference_language="auto")
        rows = get_project_subtitles(config, self.output)
        self.assertEqual((rows[0]["start"], rows[0]["end"]), (1.25, 3.25))
        self.assertFalse(rows[0]["timing_modified"])
        recorded = []
        def fake_ass(entries, path, **kwargs):
            recorded.extend([(x.start_seconds, x.display_end_seconds) for x in entries])
            path.write_text("ASS fixture", encoding="utf-8")
        with patch("app.subtitles.write_ass", side_effect=fake_ass):
            result = refresh_subtitle_artifacts_from_settings(
                self.output, generate_ass=True
            )
        self.assertEqual(result["ass_error"], "")
        self.assertEqual(recorded[0], (1.25, 3.25))
        self.assertEqual(recorded[1], (4.0, 6.0))
        srt = (self.output / "HSR_Voice_Archive.srt").read_text(encoding="utf-8-sig")
        self.assertIn("00:00:01,250 --> 00:00:03,250", srt)
        self.assertEqual(legacy.read_bytes(), before, "Historical data must not be deleted")
        self.assertEqual(json.loads(self.manifest.read_text(encoding="utf-8"))["entries"][0]["start_seconds"], 1.25)

    def test_source_audio_change_updates_outputs_even_with_old_overrides(self):
        (self.output / "subtitle_timing_overrides.json").write_text(
            json.dumps({"schema_version": 1, "entries": {"11": {"start": 1.2, "end": 3.7}}}),
            encoding="utf-8",
        )
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        data["entries"][0]["start_seconds"] = 1.85
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(effective_subtitle_window(data["entries"][0],
                                                    read_timing_overrides(self.output)),
                         (1.85, 3.25))
        result = refresh_subtitle_artifacts_from_settings(self.output, generate_ass=False)
        self.assertTrue(result["srt_file"])
        srt = (self.output / "HSR_Voice_Archive.srt").read_text(encoding="utf-8-sig")
        self.assertIn("00:00:01,850 --> 00:00:03,250", srt)
        config = SimpleNamespace(source_text_language="en", reference_language="auto")
        self.assertFalse(get_project_subtitles(config, self.output)[0]["timing_conflict"])


if __name__ == "__main__":
    unittest.main()
