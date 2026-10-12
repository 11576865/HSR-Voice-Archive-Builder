"""Synthetic long-corpus stress acceptance for bounded exported-ASS sampling.

Not real HSR game voice data; measures deterministic parser throughput and
Python allocation peak for the plan-only stage, not FFmpeg rendering time.
"""
from __future__ import annotations

import json
import os
import time
import tracemalloc
import unittest
from pathlib import Path

from app.ass_export_sampling import _parse_windows, plan_export_ass_samples


def _clock(centiseconds: int) -> str:
    hours, rest = divmod(centiseconds, 360000)
    minutes, rest = divmod(rest, 6000)
    seconds, fraction = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{fraction:02d}"


def synthetic_long_ass(windows: int = 12000) -> str:
    rows = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for i in range(windows):
        # Dense multi-layer source: same time window, two distinct active
        # Dialogue texts. One motion/fade, one word Karaoke.
        start, end = _clock(i * 275), _clock(i * 275 + 210)
        rows.append(
            rf"Dialogue: 0,{start},{end},Default,,0,0,0,,"
            rf"{{\move(30,920,960,920,0,2100)}}"
            "An archive line with long voice text to exercise real parsing"
        )
        rows.append(
            rf"Dialogue: 1,{start},{end},Default,,0,0,0,,"
            rf"{{\k20}}one{{\kf35}}two{{\k50}}three{{\fad(100,150)}}"
            "模拟中文字幕校验句"
        )
    return "\n".join(rows) + "\n"


class AssExportSamplingScaleTests(unittest.TestCase):
    def test_selected_only_text_memory_ownership(self):
        source = synthetic_long_ass(750)
        windows, starts, ends = _parse_windows(source)
        self.assertEqual(len(windows), 750)
        self.assertEqual(len(starts), 1500)
        self.assertEqual(len(ends), 1500)
        self.assertTrue(all("texts" not in window for window in windows))
        self.assertTrue(all(window["dynamic_tags"] for window in windows))

    def test_twenty_four_thousand_dialogue_stress_budget(self):
        source = synthetic_long_ass(12000)
        tracemalloc.start()
        try:
            begin = time.perf_counter()
            plan = plan_export_ass_samples(source, sample_limit=8)
            seconds = time.perf_counter() - begin
            current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        result = {
            "schema_version": 1,
            "fixture_kind": "synthetic_long_ass_not_real_game_corpus",
            "dialogue_count": plan["dialogue_count"],
            "unique_window_count": plan["unique_window_count"],
            "dynamic_window_count": plan["dynamic_window_count"],
            "sample_count": plan["sample_count"],
            "unexamined_dynamic_windows": plan["unexamined_dynamic_windows"],
            "candidate_count_scope": plan["candidate_count_scope"],
            "plan_only_seconds": round(seconds, 4),
            "python_tracemalloc_peak_mib": round(peak_bytes / 1048576, 3),
            "python_tracemalloc_current_mib": round(current_bytes / 1048576, 3),
            "ffmpeg_frames_rendered": 0,
            "full_interval_verified": False,
        }
        print("HSR_ASS_PLAN_STRESS " + json.dumps(result, sort_keys=True), flush=True)
        target = os.environ.get("HSR_ASS_SAMPLER_BENCHMARK_DIR")
        if target:
            output = Path(target)
            output.mkdir(parents=True, exist_ok=True)
            (output / "ass-export-sampling-synthetic.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        self.assertEqual(plan["dialogue_count"], 24000)
        self.assertEqual(plan["unique_window_count"], 12000)
        self.assertEqual(plan["dynamic_window_count"], 12000)
        self.assertLessEqual(plan["sample_count"], 8)
        self.assertGreaterEqual(plan["unexamined_dynamic_windows"], 11990)
        self.assertTrue(all(item["active_dialogue_count"] == 2
                            for item in plan["samples"]))
        # Generous CI guardrails: real machine performance is an
        # environment-specific observation, not a release-wide guarantee.
        self.assertLess(seconds, 45.0)
        self.assertLess(peak_bytes, 256 * 1048576)


if __name__ == "__main__":
    unittest.main()
