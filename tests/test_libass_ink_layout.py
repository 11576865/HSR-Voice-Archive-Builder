"""Real FFmpeg/libass evidence for the HSR line-break/fit solver.

The bounds oracle is independently raster-backed; no DOM, SVG or synthetic
Pillow-only geometry can fulfill these tests.
"""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from subtitle_layout.ass_writer import render_ass
from subtitle_layout.config import SubtitleRenderConfig
from subtitle_layout.font_scale import SCALE_FACTORS
from subtitle_layout.layout_solver import solve_subtitle_layout
from subtitle_layout.libass_ink import (
    InkBounds, InkProbeUnavailable, _INK_CACHE, validate_real_ink,
)
from subtitle_layout.safe_area import SafeArea


class LibassInkSolverTests(unittest.TestCase):
    def test_true_oracle_can_override_synthetic_candidate_and_select_next_scale(self):
        examined = []
        def validator(candidate):
            examined.append(candidate.scale_factor)
            return (candidate.scale_factor <= 0.95, "LIBASS_HORIZONTAL_OVERFLOW")
        result = solve_subtitle_layout(
            "This English line is long enough for validation", "",
            ink_validator=validator,
        )
        self.assertFalse(result.failed)
        self.assertEqual(examined[:2], [1.0, 0.95])
        self.assertEqual(result.scale_factor, 0.95)

    def test_oracle_rejection_propagates_ink_specific_reason(self):
        result = solve_subtitle_layout("rejected real font", "",
                ink_validator=lambda _: (False, "LIBASS_VISIBLE_INK_OVERFLOW"))
        self.assertTrue(result.failed)
        self.assertEqual(result.failed_condition, "LIBASS_VISIBLE_INK_OVERFLOW")
        self.assertEqual(len(result.scale_attempts), len(SCALE_FACTORS))

    def test_real_probe_fails_closed_when_ffmpeg_unavailable(self):
        cfg = SubtitleRenderConfig(verify_libass_layout=True)
        candidate = solve_subtitle_layout("Real ink must be measured", "")
        with patch("subtitle_layout.libass_ink.shutil.which", return_value=None):
            with self.assertRaisesRegex(InkProbeUnavailable, "FFmpeg"):
                validate_real_ink(candidate, cfg, SafeArea())

    def test_real_project_export_uses_verified_ink_not_svg(self):
        cfg = SubtitleRenderConfig(
            primary_font="DejaVu Sans",
            chs_font="DejaVu Sans",
            enable_kinetic=False,
            enable_multi_layer_outline=False,
            verify_libass_layout=True,
        )
        entry = SimpleNamespace(
            english="Actual raster-backed layout verification",
            chinese="", start_seconds=0.0, display_end_seconds=3.0,
        )
        with patch("subtitle_layout.ass_writer.validate_real_ink") as probe:
            from subtitle_layout.libass_ink import InkVerdict
            probe.return_value = InkVerdict(True, None, InkBounds(150, 500, 810, 550),
                                           None, 1.0, 1.0)
            emitted = render_ass([entry], config=cfg)
            self.assertIn("Dialogue:", emitted)
            self.assertTrue(probe.called)

    @unittest.skipUnless(shutil.which("ffmpeg"), "system FFmpeg not installed")
    def test_libass_real_rgb_frame_ink_detection(self):
        # Actual renderer fixture. The probes produce 1920x1080 rgb24 frames.
        cfg = SubtitleRenderConfig(
            primary_font="DejaVu Sans", enable_kinetic=False,
            enable_frosted_glass=False,
            enable_multi_layer_outline=False, outline_width=3.0,
            shadow_depth=2.0,
        )
        candidate = solve_subtitle_layout("Actual libass measurements", "",
                    base_primary_size=54)
        _INK_CACHE.clear()
        result = validate_real_ink(candidate, cfg, SafeArea())
        self.assertTrue(result.ok, result.reason)
        self.assertIsNotNone(result.primary)
        self.assertGreater(result.primary.width, 100)
        self.assertGreater(result.primary.height, 20)
        self.assertLess(result.primary.width, SafeArea().max_printable_width)
        self.assertAlmostEqual(result.primary_width_ratio, 1.0, delta=1.0)
        cached = validate_real_ink(candidate, cfg, SafeArea())
        self.assertEqual(cached, result)

    def test_project_flows_explicitly_enable_real_oracle(self):
        root = Path(__file__).resolve().parents[1]
        preview = (root/"app/ass_preview.py").read_text("utf-8")
        project = (root/"app/subtitles.py").read_text("utf-8")
        self.assertIn("verify_libass_layout=True", preview)
        self.assertIn("verify_libass_layout=True", project)
        self.assertIn("ink_validator=validate_ink_candidate", (
            root/"subtitle_layout/ass_writer.py").read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
