"""Integration of real ASS/SRT writers, saved cue text and timing, and retries.

Uses real generated ASS and SRT files; optional FFmpeg/libass pixel verification
renders a black frame at active and inactive cue times.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from subtitle_layout import SubtitleRenderConfig
from app.subtitle_timing import read_timing_overrides, update_subtitle_display_timing
from app.subtitles import (
    refresh_derived_subtitle_exports,
    refresh_subtitle_artifacts_from_settings,
    save_subtitle_timing_and_refresh,
    update_project_subtitles,
)


class IntegratedSubtitleExportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name)
        self.manifest = self.output / "manifest.json"
        self.manifest.write_text(json.dumps({
            "entries": [
                {
                    "index": 7, "source_member_id": "clip/dialogue-7.wav",
                    "filename": "dialogue-7.wav",
                    "source_text": "A journey begins.",
                    "target_text": "旅途开始。",
                    "start_seconds": 1.2,
                    "audio_end_seconds": 3.0,
                    "display_end_seconds": 3.0,
                },
                {
                    "index": 8, "source_member_id": "clip/dialogue-8.wav",
                    "filename": "dialogue-8.wav",
                    "source_text": "Next scene.",
                    "target_text": "下一幕。",
                    "start_seconds": 8.0,
                    "audio_end_seconds": 10.0,
                    "display_end_seconds": 10.0,
                },
            ]
        }, ensure_ascii=False), encoding="utf-8")
        self.config = SimpleNamespace(
            source_text_language="en", target_language="zh-CN", generate_ass=True
        )
        self.render_cfg = SubtitleRenderConfig(
            chs_font="DejaVu Sans", primary_font="DejaVu Sans",
            enable_kinetic=False, enable_karaoke=False,
        )
        self.config_patch = patch(
            "app.subtitles.subtitle_render_config", return_value=self.render_cfg
        )
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def read_ass(self):
        return (self.output / "HSR_Voice_Archive.ass").read_text(encoding="utf-8-sig")

    def read_srt(self):
        return (self.output / "HSR_Voice_Archive.srt").read_text(encoding="utf-8-sig")

    def test_real_ass_srt_and_source_clock_survive_timing_and_text_round_trip(self):
        saved = save_subtitle_timing_and_refresh(
            self.config, self.output, item_id=7,
            expected_start=1.2, expected_end=3.0, start=1.3, end=3.6,
        )
        self.assertTrue(saved["saved"])
        self.assertTrue(saved["artifacts_current"], saved["artifact_error"])
        self.assertIn("00:00:01,300 --> 00:00:03,600", self.read_srt())
        real_ass = self.read_ass()
        self.assertIn("[Events]", real_ass)
        self.assertIn("Dialogue:", real_ass)
        self.assertIn("0:00:01.30", real_ass)
        self.assertIn("0:00:03.60", real_ass)
        self.assertIn("A journey begins.", real_ass)

        with patch("app.subtitles.invalidate_subtitle_stages"):
            text_save = update_project_subtitles(
                self.config, self.output, [{"id": 7, "final_chs": "定稿正文"}]
            )
        self.assertTrue(text_save["saved"])
        self.assertTrue(text_save["artifacts_current"], text_save["artifact_error"])
        self.assertIn("定稿正文", self.read_srt())
        self.assertIn("定稿正文", self.read_ass())
        self.assertIn("00:00:01,300 --> 00:00:03,600", self.read_srt())

        # Intentional blank Chinese text is a valid edit, not a request to
        # silently reinsert the original target translation.
        with patch("app.subtitles.invalidate_subtitle_stages"):
            blank_save = update_project_subtitles(
                self.config, self.output, [{"id": 7, "final_chs": ""}]
            )
        self.assertTrue(blank_save["saved"])
        self.assertTrue(blank_save["artifacts_current"], blank_save["artifact_error"])
        self.assertNotIn("旅途开始。", self.read_srt())
        self.assertNotIn("旅途开始。", self.read_ass())
        self.assertIn("A journey begins.", self.read_srt())
        source = json.loads(self.manifest.read_text(encoding="utf-8"))["entries"][0]
        self.assertEqual(source["start_seconds"], 1.2)
        self.assertEqual(source["audio_end_seconds"], 3.0)
        self.assertEqual(source["display_end_seconds"], 3.0)

    def test_failed_srt_export_keeps_commit_and_independent_retry_recovers(self):
        with patch("app.subtitles.write_srt", side_effect=OSError("synthetic disk export failure")):
            result = save_subtitle_timing_and_refresh(
                self.config, self.output, item_id=7, expected_start=1.2,
                expected_end=3.0, start=1.4, end=3.4,
            )
        self.assertTrue(result["saved"])
        self.assertFalse(result["artifacts_current"])
        self.assertIn("synthetic disk export failure", result["artifact_error"])
        self.assertEqual(read_timing_overrides(self.output)["7"]["end"], 3.4)
        recovered = refresh_derived_subtitle_exports(
            self.config, self.output, force_ass=True
        )
        self.assertFalse(recovered["ass_error"])
        self.assertIn("00:00:01,400 --> 00:00:03,400", self.read_srt())
        self.assertIn("0:00:03.40", self.read_ass())

    def test_stale_overlay_cannot_touch_manifest_or_export_before_validation(self):
        update_subtitle_display_timing(
            self.output, item_id=7, expected_start=1.2, expected_end=3,
            start=1.3, end=3.2,
        )
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        data["entries"][0]["start_seconds"] = 2.0
        self.manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        old_bytes = self.manifest.read_bytes()
        with self.assertRaisesRegex(ValueError, "outdated audio timeline"):
            refresh_subtitle_artifacts_from_settings(self.output, generate_ass=False)
        self.assertEqual(self.manifest.read_bytes(), old_bytes)
        self.assertFalse((self.output / "manifest.csv").exists())
        self.assertFalse((self.output / "HSR_Voice_Archive.srt").exists())

    def test_nonexistent_cue_text_update_cannot_create_override_files(self):
        with self.assertRaisesRegex(ValueError, "No matching subtitle entries"):
            update_project_subtitles(
                self.config, self.output, [{"id": 99999, "final_chs": "orphan"}]
            )
        self.assertFalse((self.output / "subtitles_overrides.json").exists())
        self.assertFalse((self.output / "subtitle_review_state.json").exists())

    def test_real_libass_renders_only_during_updated_display_window(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg is unavailable; ASS file round-trip still tested")
        check = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if " ass " not in check.stdout:
            self.skipTest("FFmpeg build has no libass filter")
        saved = save_subtitle_timing_and_refresh(
            self.config, self.output, item_id=7, expected_start=1.2,
            expected_end=3.0, start=1.3, end=3.6,
        )
        self.assertTrue(saved["artifacts_current"], saved["artifact_error"])
        ass_path = self.output / "HSR_Voice_Archive.ass"
        def frame(at: float) -> bytes:
            # The generated ASS file, not a synthetic overlay, is rasterized
            # by FFmpeg/libass against a black source frame.
            vf = f"setpts=PTS+{at}/TB,ass={str(ass_path)}"
            done = subprocess.run(
                [ffmpeg, "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                 "color=c=black:s=640x360:r=1", "-vf", vf,
                 "-frames:v", "1", "-pix_fmt", "rgb24",
                 "-f", "rawvideo", "-"],
                capture_output=True, timeout=20, check=False,
            )
            self.assertEqual(done.returncode, 0, done.stderr[:1000])
            self.assertEqual(len(done.stdout), 640 * 360 * 3)
            return done.stdout
        before, active, after = frame(0.6), frame(2.0), frame(5.0)
        self.assertEqual(before, after)
        self.assertNotEqual(active, before, "libass did not rasterize cue at saved display time")


if __name__ == "__main__":
    unittest.main()
