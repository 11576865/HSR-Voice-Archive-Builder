"""Regression: audio-derived cue boundaries are display-only, never editable."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.subtitle_timing import update_subtitle_display_timing, effective_subtitle_window


ROOT = Path(__file__).resolve().parents[1]


class SubtitleDisplayTimingEditorTests(unittest.TestCase):
    def test_editor_does_not_advertise_writable_timing(self):
        html=(ROOT / "app/static/index.html").read_text(encoding="utf-8")
        self.assertIn('id="layoutCueClockOrigin"',html)
        self.assertIn('id="layoutCueStart"',html)
        self.assertIn('id="layoutCueEnd"',html)
        self.assertNotIn('id="layoutCueTimingSaveBtn"',html)
        self.assertNotIn('id="layoutCueTimingStart"',html)
        self.assertNotIn('id="layoutCueTimingEnd"',html)
        self.assertNotIn('data-timeline-edge="start"',html)
        self.assertIn('if(false){ // Display-only timeline',html)

    def test_legacy_saved_override_never_retimes_source(self):
        entry={"index":11,"start_seconds":2,"display_end_seconds":4,
               "source_member_id":"voice/11.wav"}
        stored={"11":{"start":2.1,"end":4.4,"source_start":2,
                      "source_end":4,"source_member_id":"voice/11.wav"}}
        self.assertEqual(effective_subtitle_window(entry,stored),(2.0,4.0))

    def test_backend_rejects_timing_writes_without_touching_archive(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            manifest=folder/"manifest.json"
            manifest.write_text(json.dumps({"entries":[{"index":11,
                "start_seconds":2,"display_end_seconds":4}]}),encoding="utf-8")
            before=manifest.read_bytes()
            with self.assertRaisesRegex(ValueError,"音频时序"):
                update_subtitle_display_timing(folder,item_id=11,start=2.1,end=4.2,
                    expected_start=2,expected_end=4)
            self.assertEqual(manifest.read_bytes(),before)
            self.assertFalse((folder/"subtitle_timing_overrides.json").exists())


if __name__=="__main__":
    unittest.main()
