from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.ass_preview import _preview_times, _render_raw_rgb
from app.security import api_token
from app.server import app
from subtitle_layout.rendered_ink import visible_ink_bbox


def _pixel(frame: bytearray, width: int, x: int, y: int, rgb: tuple[int, int, int]):
    at = 3 * (y * width + x)
    frame[at:at + 3] = bytes(rgb)


class RenderedInkUnitTests(unittest.TestCase):
    def test_dual_background_captures_both_white_glyph_and_black_shadow(self):
        width, height = 8, 6
        black = bytearray(width * height * 3)
        white = bytearray([255] * width * height * 3)
        _pixel(black, width, 2, 1, (255, 255, 255))
        _pixel(white, width, 2, 1, (255, 255, 255))
        _pixel(black, width, 7, 4, (0, 0, 0))
        _pixel(white, width, 7, 4, (0, 0, 0))
        self.assertEqual(visible_ink_bbox(black, white, width, height), (2, 1, 8, 5))

    def test_cutoff_and_empty_status_are_explicit(self):
        width, height = 3, 2
        black = bytearray(width * height * 3)
        white = bytearray([255] * width * height * 3)
        _pixel(black, width, 1, 1, (10, 10, 10))
        _pixel(white, width, 1, 1, (245, 245, 245))
        self.assertIsNone(visible_ink_bbox(black, white, width, height, threshold=16))
        self.assertEqual(visible_ink_bbox(black, white, width, height, threshold=8), (1, 1, 2, 2))
        with self.assertRaises(ValueError):
            visible_ink_bbox(bytes(3), bytes(3), width, height)

    def test_nonfinite_timestamps_are_rejected(self):
        with self.assertRaises(ValueError):
            _preview_times(2, float("nan"))
        with self.assertRaises(ValueError):
            _preview_times(float("inf"), 1)

    def test_api_reports_single_frame_scope(self):
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        fake = {
            "ok": True, "renderer": "ffmpeg-libass",
            "scope": "single_timestamp_only", "timestamp_seconds": 0.6,
            "status": "outside_safe_area", "bbox": [1, 2, 8, 12],
        }
        with patch("app.server.inspect_rendered_ink_frame", return_value=fake) as probe:
            resp = client.post(
                "/api/subtitle-layout/inspect-rendered-ink",
                headers={"X-HSR-Token": api_token()},
                json={"english_text": "Hello", "preview_timestamp": 0.6},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "outside_safe_area")
        self.assertEqual(probe.call_args.kwargs["timestamp"], 0.6)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    def test_real_libass_fractional_timebase_changes_kinetic_position(self):
        ass = r"""[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 180
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,3,2,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,{\move(30,130,290,130,0,2000)}HELLO
"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "timing.ass"
            path.write_text(ass, "utf-8")
            early = _render_raw_rgb("ffmpeg", path, timestamp=0.10, background="black", width=320, height=180)
            late = _render_raw_rgb("ffmpeg", path, timestamp=0.80, background="black", width=320, height=180)
            self.assertNotEqual(early, late, "Real libass frame must change at subsecond timestamps")


if __name__ == "__main__":
    unittest.main()
