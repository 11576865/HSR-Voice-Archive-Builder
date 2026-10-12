from __future__ import annotations

import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.ass_preview import inspect_rendered_ink_interval
from app.security import api_token
from app.server import app
from subtitle_layout.config import SubtitleRenderConfig
from subtitle_layout.temporal_samples import plan_temporal_samples, MAX_SAMPLE_COUNT


class RenderedInkIntervalTests(unittest.TestCase):
    def test_plan_is_bounded_and_covers_endpoints(self):
        result = plan_temporal_samples(5.0, enable_kinetic=True, fade_in_ms=200,
                                       fade_out_ms=300, sample_limit=8)
        times = [point["timestamp_seconds"] for point in result["samples"]]
        self.assertEqual(times[0], 0)
        self.assertEqual(times[-1], 4.99)
        self.assertIn(2.495, times)
        self.assertEqual(len(times), 8)
        self.assertEqual(times, sorted(set(times)))
        self.assertGreater(result["omitted_candidate_count"], 0)
        self.assertGreater(result["max_unsampled_gap_seconds"], 0)

    def test_word_edges_and_bounds(self):
        words = [{"start": i * .05, "end": (i + 1) * .05} for i in range(80)]
        plan = plan_temporal_samples(5, word_alignments=words, enable_karaoke=True, sample_limit=10)
        self.assertEqual(plan["recognized_word_count"], 80)
        self.assertEqual(len(plan["samples"]), 10)
        self.assertTrue(any(
            {"word_start", "word_end"} & set(point["reasons"])
            for point in plan["samples"]
        ))
        overloaded = plan_temporal_samples(2, word_alignments=[
            {"start": 0, "end": .001} for _ in range(501)
        ], enable_karaoke=True)
        self.assertTrue(overloaded["word_alignment_input_truncated"])
        self.assertEqual(overloaded["recognized_word_count"], 500)
        self.assertLessEqual(len(overloaded["samples"]), MAX_SAMPLE_COUNT)

    def test_unbounded_sample_limits_fail_closed(self):
        for value in (0, 2, 13, 999999, 4.5, True, "8"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                plan_temporal_samples(3, sample_limit=value)

    @patch("app.ass_preview.visible_ink_bbox")
    @patch("app.ass_preview._render_raw_rgb", return_value=b"rgb")
    @patch("app.ass_preview._write_preview_ass")
    @patch("app.ass_preview.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_one_ass_many_frames_and_violation(self, which, writer, render, bbox):
        bbox.side_effect = [(100, 100, 200, 200), (0, 2, 5, 6)] + [None] * 6
        res = inspect_rendered_ink_interval(
            english_text="Hello", chinese_text="你好", source_language="en",
            target_language="zh-CN", config=SubtitleRenderConfig(enable_kinetic=False),
            duration_seconds=3, sample_limit=8,
        )
        self.assertEqual(res["status"], "sampled_violation")
        self.assertIs(res["full_interval_verified"], False)
        self.assertEqual(res["violation_count"], 1)
        self.assertEqual(res["sample_count"], 8)
        self.assertEqual(res["no_visible_ink_count"], 6)
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(render.call_count, 16)
        self.assertEqual(res["safe_area"]["x_min"], 58)

    @patch("app.ass_preview.visible_ink_bbox", return_value=None)
    @patch("app.ass_preview._render_raw_rgb", return_value=b"rgb")
    @patch("app.ass_preview._write_preview_ass")
    @patch("app.ass_preview.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_blank_frames_are_inconclusive(self, which, writer, render, bbox):
        res = inspect_rendered_ink_interval(
            english_text="Hello", chinese_text="", source_language="en",
            target_language="zh-CN", config=SubtitleRenderConfig(),
            duration_seconds=2, sample_limit=3,
        )
        self.assertEqual(res["status"], "no_visible_evidence")
        self.assertEqual(res["visible_sample_count"], 0)
        self.assertIs(res["full_interval_verified"], False)

    @patch("app.ass_preview._render_raw_rgb", side_effect=RuntimeError("renderer failed"))
    @patch("app.ass_preview._write_preview_ass")
    @patch("app.ass_preview.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_renderer_failure_cannot_be_partial_success(self, which, writer, render):
        with self.assertRaisesRegex(RuntimeError, "renderer failed"):
            inspect_rendered_ink_interval(
                english_text="Hello", chinese_text="你好", source_language="en",
                target_language="zh-CN", config=SubtitleRenderConfig(),
                duration_seconds=3, sample_limit=3,
            )

    @patch("app.server.inspect_rendered_ink_interval")
    def test_api_reports_explicit_sampled_scope(self, inspect):
        inspect.return_value = {"ok": True, "scope": "bounded_sampled_timestamps_only",
                                  "full_interval_verified": False, "status": "sampled_no_violation"}
        response = TestClient(app, base_url="http://127.0.0.1:8765").post(
            "/api/subtitle-layout/inspect-rendered-interval",
            headers={"X-HSR-Token": api_token()},
            json={"english_text": "Hello", "preview_duration_seconds": 4.5, "sample_limit": 3},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["full_interval_verified"], False)
        self.assertEqual(inspect.call_args.kwargs["sample_limit"], 3)

    def test_workbench_declares_coverage_limits(self):
        from pathlib import Path
        html = (Path(__file__).resolve().parents[1] / "app" / "static" / "index.html").read_text("utf-8")
        self.assertIn('id="previewInkSpanBtn"', html)
        self.assertIn("async function inspectRenderedInkInterval()", html)
        self.assertIn("full_interval_verified!==false", html)
        self.assertIn("omitted_candidate_count", html)
        self.assertIn("invalidateRenderedInkCheck()", html)


if __name__ == "__main__":
    unittest.main()
