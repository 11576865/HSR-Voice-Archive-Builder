"""Silence between recorded voice clips must contain no subtitle events."""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.subtitle_timing import source_subtitle_window
from app.subtitles import _subtitle_adapters
from app.timeline import resolve_timeline, write_srt
from subtitle_layout.ass_writer import render_ass
from subtitle_layout.config import SubtitleRenderConfig


class VoiceOnlySubtitleWindowTests(unittest.TestCase):
    def test_timeline_retains_actual_silence_but_does_not_hold_subtitles(self):
        report = resolve_timeline(
            [
                {"index": 1, "filename": "first.wav", "group": "a", "source_frames": 1000},
                {"index": 2, "filename": "second.wav", "group": "b", "source_frames": 2000},
            ],
            sample_rate=1000, intro_gap=1.0,
            same_group_gap=1.5, group_gap=3.0,
        )
        a, b = report["entry_timings"]
        self.assertEqual(a["start_sample"], 1000)
        self.assertEqual(a["audio_end_sample"], 2000)
        self.assertEqual(a["display_end_sample"], 2000)
        self.assertEqual(a["next_start_sample"], 5000)
        self.assertEqual(b["display_end_sample"], b["audio_end_sample"])
        self.assertTrue(any(
            gap["type"] == "gap" and gap["start_sample"] == 2000
            and gap["end_sample"] == 5000
            for gap in report["segments"]
        ))

    def test_previous_project_legacy_display_hold_is_ignored_by_ui_srt_and_ass(self):
        source = [
            {"index": 1, "start_seconds": 1.0,
             "audio_end_seconds": 2.0, "display_end_seconds": 4.0,
             "source_text": "Hello", "target_text": "你好"},
            {"index": 2, "start_seconds": 5.0,
             "audio_end_seconds": 6.0, "display_end_seconds": 7.0,
             "source_text": "Next", "target_text": "下一条"},
        ]
        self.assertEqual(source_subtitle_window(source[0]), (1.0, 2.0))
        self.assertEqual(source[0]["display_end_seconds"], 4.0)  # no source mutation
        adapters = _subtitle_adapters(source)
        self.assertEqual(
            [(x.start_seconds, x.display_end_seconds) for x in adapters],
            [(1.0, 2.0), (5.0, 6.0)],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice-only.srt"
            write_srt(adapters, path)
            srt = path.read_text(encoding="utf-8-sig")
        self.assertIn("00:00:01,000 --> 00:00:02,000", srt)
        self.assertIn("00:00:05,000 --> 00:00:06,000", srt)
        self.assertNotIn("00:00:04,000", srt)

    def test_ass_fades_use_silence_without_shortening_voice_or_karaoke(self):
        cfg = SubtitleRenderConfig(
            enable_karaoke=True, karaoke_mode="kf", karaoke_timing="cue",
            karaoke_unit="character", enable_kinetic=True,
            use_audio_aware_fade=True, fade_in_ms=200, fade_out_ms=200,
            verify_libass_layout=False,
        )
        entries = [
            SimpleNamespace(english="Hello", chinese="你好",
                            start_seconds=1.0, audio_end_seconds=2.0,
                            display_end_seconds=4.0),
            SimpleNamespace(english="Next", chinese="下一条",
                            start_seconds=5.0, audio_end_seconds=6.0,
                            display_end_seconds=7.0),
        ]
        ass = render_ass(entries, config=cfg)
        lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        self.assertTrue(lines)
        # 9: onset minus 200ms, voice 1.00-2.00, outgoing until 2.20.
        # Next onset at 5.00 with its fade starting at 4.80; no last tail.
        self.assertEqual(
            {(line.split(",", 3)[1], line.split(",", 3)[2]) for line in lines},
            {("0:00:00.80", "0:00:02.20"),
             ("0:00:04.80", "0:00:06.20")},
        )
        first = next(line for line in lines if ",Primary," in line
                     and "0:00:00.80," in line)
        last = next(line for line in lines if ",Primary," in line
                    and "0:00:04.80," in line)
        self.assertIn(r"\fad(200,200)", first)
        self.assertIn(r"\fad(200,200)", last)
        self.assertIn(r"{\kt20}", first)
        self.assertIn(r"{\kt20}", last)
        self.assertEqual(
            sum(int(t) for t in re.findall(r"\{\\kf(\d+)\}", first)),
            100, "the speech itself still gets its full 100cs karaoke budget",
        )

    def test_short_gap_rejects_configured_fade_without_silent_clamping(self):
        cfg = SubtitleRenderConfig(enable_kinetic=True, fade_in_ms=200,
                                   fade_out_ms=200, verify_libass_layout=False)
        entries = [
            SimpleNamespace(english="Alpha", chinese="甲",
                            start_seconds=1.0, audio_end_seconds=2.0,
                            display_end_seconds=2.09),
            SimpleNamespace(english="Beta", chinese="乙",
                            start_seconds=2.1, audio_end_seconds=3.0,
                            display_end_seconds=3.0),
        ]
        with self.assertRaisesRegex(ValueError, "请减小 Fade 时长"):
            render_ass(entries, config=cfg)
        # Setting a valid 50ms symmetric fade occupies exactly 100ms.
        cfg.fade_in_ms = 50
        cfg.fade_out_ms = 50
        ass = render_ass(entries, config=cfg)
        first = next(l for l in ass.splitlines() if "Alpha" in l and l.startswith("Dialogue:"))
        second = next(l for l in ass.splitlines() if "Beta" in l and l.startswith("Dialogue:"))
        self.assertIn("0:00:00.95,0:00:02.05", first)
        self.assertIn("0:00:02.05,0:00:03.05", second)
        self.assertIn(r"\fad(50,50)", first)
        self.assertIn(r"\fad(50,50)", second)

    def test_non_kinetic_ass_and_srt_stay_at_voice_boundaries(self):
        entries = [
            SimpleNamespace(english="Just audio", chinese="只有语音",
                            start_seconds=1.0, audio_end_seconds=2.0,
                            display_end_seconds=4.0),
        ]
        ass = render_ass(entries, config=SubtitleRenderConfig.plain_text_preset())
        self.assertIn("0:00:01.00,0:00:02.00", ass)
        self.assertNotIn(r"\fad(", ass)
        self.assertNotIn("0:00:04.00", ass)

    def test_first_fade_honors_audio_zero_and_last_fade_extends_into_tail(self):
        cfg = SubtitleRenderConfig(enable_kinetic=True, fade_in_ms=300,
                                   fade_out_ms=300, verify_libass_layout=False)
        one = SimpleNamespace(english="Only", chinese="唯一",
                              start_seconds=0.0, audio_end_seconds=1.0,
                              display_end_seconds=1.0)
        ass = render_ass([one], config=cfg)
        primary = next(l for l in ass.splitlines() if "Only" in l and l.startswith("Dialogue:"))
        self.assertIn("0:00:00.00,0:00:01.30", primary)
        self.assertIn(r"\fad(0,300)", primary)


if __name__ == "__main__":
    unittest.main()
