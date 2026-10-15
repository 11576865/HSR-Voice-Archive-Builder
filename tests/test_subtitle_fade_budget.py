"""Project fade envelope budgets: 60% advisory, 100% no-increase guard."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.subtitle_fade_budget import fade_budget, validate_fade_update


class FadeBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.config = SimpleNamespace(
            root=str(self.root), output_dir="output",
            same_group_gap=1.5, group_gap=5.0,
            subtitle_fade_in_ms=200, subtitle_fade_out_ms=200,
        )

    def set_entries(self, entries):
        (self.output / "manifest.json").write_text(
            json.dumps({"entries": entries}), encoding="utf-8",
        )

    def test_configured_short_gap_wins_over_chapter_gap_before_build(self):
        data = fade_budget(self.config)
        self.assertEqual(data["min_gap_ms"], 1500)
        self.assertEqual(data["source"], "configured_shorter_gap")
        self.assertFalse(data["warning"])

    def test_real_shortest_gap_wins_over_large_chapter_gap(self):
        self.set_entries([
            {"index": 1, "group": "a", "start_seconds": 1.0, "audio_end_seconds": 2.0},
            {"index": 2, "group": "a", "start_seconds": 2.7, "audio_end_seconds": 3.5},
            {"index": 3, "group": "b", "start_seconds": 8.5, "audio_end_seconds": 9.5},
            {"index": 4, "group": "b", "start_seconds": 10.2, "audio_end_seconds": 11.0},
        ])
        data = fade_budget(self.config)
        self.assertEqual(data["min_gap_ms"], 700)
        self.assertEqual(data["source"], "actual_minimum_adjacent_pcm")
        self.assertFalse(data["warning"])
        self.assertEqual(data["remaining_clear_ms"], 300)
        self.assertRaisesRegex(ValueError, "最短相邻语音间隔",
                               validate_fade_update, self.config, 500, 300)
        self.assertFalse(validate_fade_update(self.config, 350, 350)["exceeds"])

    def test_warning_strictly_above_sixty_percent_and_equal_hundred_allowed(self):
        self.set_entries([
            {"index": 1, "group": "a", "start_seconds": 0.0, "audio_end_seconds": 1.0},
            {"index": 2, "group": "a", "start_seconds": 2.0, "audio_end_seconds": 3.0},
        ])
        self.config.subtitle_fade_in_ms = 300
        self.config.subtitle_fade_out_ms = 300
        self.assertFalse(fade_budget(self.config)["warning"])
        self.config.subtitle_fade_out_ms = 301
        self.assertTrue(fade_budget(self.config)["warning"])
        self.config.subtitle_fade_out_ms = 700
        result = fade_budget(self.config)
        self.assertTrue(result["warning"])
        self.assertFalse(result["exceeds"])
        self.assertEqual(result["remaining_clear_ms"], 0)
        validate_fade_update(self.config, 500, 500)
        with self.assertRaises(ValueError):
            validate_fade_update(self.config, 500, 501)

    def test_old_over_budget_project_can_reduce_even_if_still_above_limit(self):
        self.config.subtitle_fade_in_ms = 2000
        self.config.subtitle_fade_out_ms = 1600
        self.assertTrue(fade_budget(self.config)["exceeds"])
        validate_fade_update(self.config, 1600, 1600)
        with self.assertRaises(ValueError):
            validate_fade_update(self.config, 2100, 1600)

    def test_zero_silence_blocks_positive_fade_increase(self):
        self.set_entries([
            {"index": 1, "start_seconds": 0, "audio_end_seconds": 1},
            {"index": 2, "start_seconds": 1, "audio_end_seconds": 2},
        ])
        self.assertEqual(fade_budget(self.config)["min_gap_ms"], 0)
        with self.assertRaises(ValueError):
            validate_fade_update(self.config, 300, 300)
        validate_fade_update(self.config, 0, 0)

    def test_shorter_chapter_gap_is_also_guarded(self):
        self.config.group_gap = 0.25
        self.assertEqual(fade_budget(self.config)["min_gap_ms"], 250)
        self.set_entries([
            {"group": "first", "start_seconds": 0, "audio_end_seconds": 1},
            {"group": "second", "start_seconds": 1.3, "audio_end_seconds": 2},
        ])
        self.assertEqual(fade_budget(self.config)["min_gap_ms"], 300)


if __name__ == "__main__":
    unittest.main()
