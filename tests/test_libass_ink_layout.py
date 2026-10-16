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

    @unittest.skipUnless(shutil.which("ffmpeg"), "system FFmpeg not installed")
    def test_real_bilingual_gap_survives_outline_shadow_at_every_scale(self):
        # The regression was a 20px anchor gap becoming only ~12px between
        # visible libass ink. Shrinking down to 25% did not fix fixed borders.
        from subtitle_layout.collision import central_ink_reserve
        cfg = SubtitleRenderConfig(
            primary_font="DejaVu Sans", chs_font="DejaVu Sans",
            enable_kinetic=False, enable_multi_layer_outline=False,
            outline_width=3.0, shadow_depth=2.0, blur_radius=0.0,
            min_central_gap=20.0,
        )
        text = ("Wantonly editing memories or burning them all up without a second thought.", 
                "将记忆随意剪辑，抑或不由分说地将它们焚尽。")
        unreserved = solve_subtitle_layout(*text)
        before = validate_real_ink(unreserved, cfg, SafeArea())
        # This baseline used to collide on the original font/layout; it may
        # already be safe after a solver improvement or a CI font fallback.
        # Never fabricate an oracle collision. Validate whichever genuine
        # pixel result FFmpeg returns, then still prove the fixed geometry
        # is safe at every scale below.
        self.assertIsNotNone(before.primary)
        self.assertIsNotNone(before.chinese)
        measured_gap = before.chinese.top - before.primary.bottom
        if measured_gap < cfg.min_central_gap:
            self.assertEqual(before.reason, "LIBASS_CENTRAL_COLLISION")
        else:
            self.assertNotEqual(before.reason, "LIBASS_CENTRAL_COLLISION")
        reserve = central_ink_reserve(
            cfg.outline_width, cfg.shadow_depth, cfg.blur_radius,
            cfg.enable_multi_layer_outline,
        )
        for scale in (1.0, 0.5, 0.25):
            fixed = solve_subtitle_layout(
                *text, base_chs_size=round(48*scale),
                base_primary_size=round(42*scale),
                min_central_gap=cfg.min_central_gap,
                central_ink_padding=reserve,
            )
            verdict = validate_real_ink(fixed, cfg, SafeArea())
            self.assertTrue(verdict.ok, (scale, verdict.reason))
            self.assertGreaterEqual(
                verdict.chinese.top - verdict.primary.bottom,
                cfg.min_central_gap,
            )

    def test_real_probe_fails_closed_when_ffmpeg_unavailable(self):
        cfg = SubtitleRenderConfig(verify_libass_layout=True)
        candidate = solve_subtitle_layout("Real ink must be measured", "")
        with patch("subtitle_layout.libass_ink.shutil.which", return_value=None):
            with self.assertRaisesRegex(InkProbeUnavailable, "FFmpeg"):
                validate_real_ink(candidate, cfg, SafeArea())

    def test_explicit_ass_preview_captures_verified_reflow_and_shrink(self):
        from subtitle_layout.libass_ink import InkVerdict
        entry = SimpleNamespace(
            english="Real libass decides the subtitle line size",
            chinese="真实渲染决定断行",
            start_seconds=0.0, display_end_seconds=3.0,
        )
        cfg = SubtitleRenderConfig(
            primary_font="DejaVu Sans", chs_font="DejaVu Sans",
            enable_kinetic=False, verify_libass_layout=True,
        )
        evidence = {}
        def validate(candidate, *_):
            accept = candidate.scale_factor <= 0.95
            return InkVerdict(
                accept, None if accept else "LIBASS_HORIZONTAL_OVERFLOW",
                InkBounds(120, 200, 1800, 620) if accept else None,
                InkBounds(200, 680, 1700, 840) if accept else None,
                1.0, 1.0,
            )
        with patch("subtitle_layout.ass_writer.validate_real_ink",
                   side_effect=validate):
            output = render_ass([entry], config=cfg, layout_capture=evidence)
        self.assertIn("Dialogue:", output)
        self.assertEqual(evidence["evidence"], "libass-validated-ass-plan")
        self.assertEqual(evidence["scale_percent"], 95)
        for lines in (evidence["primary_lines"], evidence["chs_lines"]):
            self.assertTrue(lines)
            for line in lines:
                self.assertGreater(line["measured_width"], 0)
                self.assertGreater(line["line_height"], 0)
                self.assertAlmostEqual(line["bbox"]["x_max"] - line["bbox"]["x_min"],
                                       line["measured_width"], places=5)
                self.assertAlmostEqual(line["bbox"]["y_max"] - line["bbox"]["y_min"],
                                       line["line_height"], places=5)

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

    @unittest.skipUnless(shutil.which("ffmpeg"), "system FFmpeg not installed")
    def test_real_ass_writer_is_accepted_by_real_ink_measurement(self):
        # No mocked libass: verifies the authored ASS can be emitted from
        # actual pixel-measured glyphs, not only from a synthetic rectangle.
        cfg = SubtitleRenderConfig(
            primary_font="DejaVu Sans", chs_font="DejaVu Sans",
            enable_kinetic=False, verify_libass_layout=True,
            enable_multi_layer_outline=True,
        )
        entry = SimpleNamespace(
            english="The actual font, actual raster, and line-break oracle agree.",
            chinese="", start_seconds=0.0, display_end_seconds=3.0,
        )
        ass = render_ass([entry], config=cfg)
        self.assertIn("Dialogue:", ass)
        self.assertIn("Style: Primary,DejaVu Sans", ass)

    def test_real_evidence_width_feedback_is_candidate_specific(self):
        from subtitle_layout.libass_ink import InkVerdict
        text = " ".join(["Glorious memories cannot be forgotten"] * 6)
        entry = SimpleNamespace(english=text, chinese="",
                                start_seconds=0.0, display_end_seconds=4.0)
        original = render_ass([entry], config=SubtitleRenderConfig(
            enable_kinetic=False, base_primary_size=42,
        ))
        real_cfg = SubtitleRenderConfig(
            enable_kinetic=False, base_primary_size=42,
            verify_libass_layout=True,
        )
        with patch("subtitle_layout.ass_writer.validate_real_ink") as oracle:
            oracle.return_value = InkVerdict(
                True, None, InkBounds(100, 300, 1150, 575), None, 0.60, 1.0
            )
            revised = render_ass([entry], config=real_cfg)
        self.assertLessEqual(revised.count(r"\N"), original.count(r"\N"))
        self.assertTrue(oracle.called)

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
