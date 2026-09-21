from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.timeline import render_ass, resolve_timeline


class TimelineTests(unittest.TestCase):
    def test_intro_and_group_gaps_use_integer_samples(self) -> None:
        rows = [
            {"index": 1, "group": "a", "filename": "a.wav", "source_frames": 8000},
            {"index": 2, "group": "a", "filename": "b.wav", "source_frames": 4000},
            {"index": 3, "group": "b", "filename": "c.wav", "source_frames": 2000},
        ]
        result = resolve_timeline(
            rows,
            8000,
            intro_gap=5.0,
            same_group_gap=0.4,
            group_gap=1.2,
        )

        timings = result["entry_timings"]
        self.assertEqual(timings[0]["start_sample"], 40000)
        self.assertEqual(timings[1]["start_sample"], 40000 + 8000 + 3200)
        self.assertEqual(
            timings[2]["start_sample"],
            40000 + 8000 + 3200 + 4000 + 9600,
        )
        self.assertEqual(result["segments"][0]["role"], "intro")
        self.assertEqual(result["total_samples"], timings[2]["audio_end_sample"])

    def test_replacing_voice_length_moves_later_entries(self) -> None:
        base = [
            {"index": 1, "group": "a", "filename": "a.wav", "source_frames": 100},
            {"index": 2, "group": "a", "filename": "b.wav", "source_frames": 100},
        ]
        first = resolve_timeline(base, 100, intro_gap=5, same_group_gap=1)
        base[0]["source_frames"] = 300
        second = resolve_timeline(base, 100, intro_gap=5, same_group_gap=1)

        self.assertEqual(
            second["entry_timings"][1]["start_sample"]
            - first["entry_timings"][1]["start_sample"],
            200,
        )

    def test_empty_timeline_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one"):
            resolve_timeline([], 48000)

    def test_ass_uses_one_chinese_line_for_chinese_primary_audio(self) -> None:
        entry = SimpleNamespace(
            english="你好。",
            chinese="你好。",
            start_seconds=5.0,
            display_end_seconds=6.0,
        )
        text = render_ass(
            [entry],
            source_language="zh-CN",
            target_language="zh-CN",
        )
        dialogues = [line for line in text.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(len(dialogues), 1)
        self.assertIn("你好。", dialogues[0])
        self.assertNotIn(r"\N", dialogues[0])

    def test_ass_uses_independent_dialogue_events_for_bilingual_audio(self) -> None:
        entry = SimpleNamespace(
            english="Hello.",
            chinese="你好。",
            start_seconds=5.0,
            display_end_seconds=6.0,
        )
        text = render_ass([entry], source_language="en", target_language="zh-CN")
        dialogues = [line for line in text.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(len(dialogues), 2)
        chs_line = next(d for d in dialogues if "CHS" in d)
        pri_line = next(d for d in dialogues if "Primary" in d)
        self.assertIn("你好。", chs_line)
        self.assertIn("Hello.", pri_line)
        self.assertNotIn(r"\N", text)


if __name__ == "__main__":
    unittest.main()
