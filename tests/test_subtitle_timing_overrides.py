"""Output-only subtitle timing: validation, non-destructive provenance, export.

Times are a derived subtitle display layer; source audio/manifest times remain
untouched, and old/stale source-time snapshots must not silently retime new audio.
"""
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

    def update(self, **fields):
        return update_subtitle_display_timing(
            self.output,
            item_id=fields.pop("item_id", 11),
            expected_start=fields.pop("expected_start", 1.25),
            expected_end=fields.pop("expected_end", 3.25),
            **fields,
        )

    def test_timing_overlay_is_applied_to_srt_and_ass_without_manifest_mutation(self):
        result = self.update(start=1.10, end=3.60)
        self.assertTrue(result["timing_modified"])
        self.assertEqual(result["start"], 1.1)
        self.assertEqual(read_timing_overrides(self.output)["11"]["end"], 3.6)
        self.assertEqual(
            json.loads(self.manifest.read_text(encoding="utf-8")), self.original
        )
        config = SimpleNamespace(source_text_language="en", reference_language="auto")
        rows = get_project_subtitles(config, self.output)
        self.assertEqual((rows[0]["start"], rows[0]["end"]), (1.1, 3.6))
        self.assertEqual((rows[0]["source_start"], rows[0]["source_end"]), (1.25, 3.25))
        self.assertTrue(rows[0]["timing_modified"])
        self.assertFalse(rows[0]["word_alignment_ready"])
        self.assertEqual((rows[1]["start"], rows[1]["end"]), (4.0, 6.0))

        captured = []
        def fake_ass(entries, path, **kwargs):
            captured.append([(x.id, x.start_seconds, x.display_end_seconds, x.word_alignments) for x in entries])
            path.write_text("ASS fixture", encoding="utf-8")

        with patch("app.subtitles.write_ass", side_effect=fake_ass):
            refreshed = refresh_subtitle_artifacts_from_settings(
                self.output, generate_ass=True, strict_ass=True
            )
        self.assertEqual(refreshed["ass_error"], "")
        self.assertEqual(captured[0][0][:3], (11, 1.1, 3.6))
        self.assertEqual(captured[0][1][:3], (12, 4.0, 6.0))
        srt = (self.output / "HSR_Voice_Archive.srt").read_text(encoding="utf-8-sig")
        self.assertIn("00:00:01,100 --> 00:00:03,600", srt)
        self.assertIn("00:00:04,000 --> 00:00:06,000", srt)
        self.assertEqual(
            json.loads(self.manifest.read_text(encoding="utf-8"))["entries"][0]["start_seconds"],
            1.25,
        )

    def test_reset_returns_to_audio_owned_clock_without_erasing_other_overrides(self):
        self.update(start=1.3, end=3.5)
        self.update(item_id=12, start=4.1, end=6.1, expected_start=4, expected_end=6)
        reverted = self.update(reset=True, expected_start=1.3, expected_end=3.5)
        self.assertFalse(reverted["timing_modified"])
        self.assertEqual((reverted["start"], reverted["end"]), (1.25, 3.25))
        self.assertNotIn("11", read_timing_overrides(self.output))
        self.assertIn("12", read_timing_overrides(self.output))

    def test_invalid_range_and_stale_request_do_not_mutate_saved_overlay(self):
        for start, end in ((-1, 1), (2.5, 2.5), (7.0, 9.0), (float("nan"), 3.1)):
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValueError):
                    self.update(start=start, end=end)
                self.assertEqual(read_timing_overrides(self.output), {})
        self.update(start=1.2, end=3.2)
        with self.assertRaisesRegex(ValueError, "changed since"):
            self.update(start=1.1, end=3.3)
        self.assertEqual(read_timing_overrides(self.output)["11"]["start"], 1.2)

    def test_source_manifest_change_invalidates_stored_display_override(self):
        self.update(start=1.2, end=3.2)
        data=json.loads(self.manifest.read_text(encoding="utf-8"))
        data["entries"][0]["start_seconds"]=1.75
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outdated audio timeline"):
            effective_subtitle_window(data["entries"][0], read_timing_overrides(self.output))
        with self.assertRaisesRegex(ValueError, "outdated audio timeline"):
            refresh_subtitle_artifacts_from_settings(self.output, generate_ass=False)

    def test_unknown_or_ambiguous_cue_id_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            self.update(item_id=99, start=1.1, end=3.1)
        data=json.loads(self.manifest.read_text(encoding="utf-8"))
        data["entries"].append(dict(data["entries"][0]))
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            self.update(start=1.1, end=3.1)


if __name__ == "__main__":
    unittest.main()
