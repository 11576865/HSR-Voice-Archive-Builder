"""FFmpeg-native HSR gradient controls, preview and complete MKV evidence."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app.archive_bundle_backgrounds import (
    DEFAULT_COLORS, gradient_source, normalize_bundle_background,
    render_background_preview,
)
from app.archive_bundle_exporter import ArchiveBundleExporter
from app.archive_bundle_lifecycle import bundle_export_status, previous_bundle_settings
from app.subtitle_export_status import record_subtitle_export


class BackgroundModelTests(unittest.TestCase):
    def test_modes_quality_defaults_and_injection_are_bounded(self):
        expected = {"color", "image", "linear", "radial", "soft"}
        for mode in expected:
            spec = normalize_bundle_background({"mode": mode})
            self.assertEqual(spec["mode"], mode)
            self.assertEqual(spec["resolution"], "720p")
            self.assertEqual(spec["quality"], "standard")
        for broken in (
            {"mode": "linear; movie=external"},
            {"direction": "horizontal:evil=1"},
            {"first": "#abcdef:movie=external"},
            {"first": "not-a-color"},
            {"resolution": "8k"},
            {"quality": "lossless:hide_banner"},
        ):
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    normalize_bundle_background(broken)

    def test_explicit_deterministic_filter_parameters(self):
        for mode in ("linear", "radial", "soft"):
            spec = normalize_bundle_background({"mode": mode, "direction": "horizontal"})
            graph, blur = gradient_source(spec, width=320, height=180, fps=1)
            self.assertIn("speed=0", graph)
            self.assertIn("seed=1", graph)
            self.assertIn("r=1", graph)
            self.assertNotIn("#", graph)
            self.assertIn("c0=" + DEFAULT_COLORS[0][1:], graph)
            if mode == "linear":
                self.assertIn("x1=320:y1=0", graph)
            if mode == "soft":
                self.assertIn("n=3", graph)
                self.assertIn("gblur", blur)
            else:
                self.assertIsNone(blur)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Real FFmpeg and FFprobe needed")
class RealGradientEvidence(unittest.TestCase):
    @staticmethod
    def _audio(root: Path) -> Path:
        audio = root / "continuous.flac"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-y", "-loglevel", "error", "-f", "lavfi",
            "-i", "sine=frequency=550:sample_rate=44100:duration=1.5",
            "-c:a", "flac", str(audio),
        ], check=True, capture_output=True)
        return audio

    @staticmethod
    def _ass(root: Path) -> Path:
        ass = root / "HSR_Voice_Archive.ass"
        ass.write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 180\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,Arial,22,&H00FFFFFF,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,20,20,20,1\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:01.40,Default,,0,0,0,,Test gradient\n",
            encoding="utf-8",
        )
        (root / "manifest.json").write_text('{"entries":[]}', encoding="utf-8")
        (root / "HSR_Voice_Archive.srt").write_text("1\n", encoding="utf-8")
        record_subtitle_export(root, state="current", ass_required=True)
        return ass

    def test_ffmpeg_preview_is_actual_png_all_modes(self):
        for mode in ("color", "linear", "radial", "soft"):
            with self.subTest(mode=mode):
                spec = normalize_bundle_background({"mode": mode})
                png = render_background_preview(spec, color="#102030")
                self.assertTrue(png.startswith(bytes.fromhex("89504e470d0a1a0a")))
                self.assertGreater(len(png), 100)

    def test_real_mkv_gradients_preserve_tracks_and_saved_settings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio = self._audio(root)
            ass = self._ass(root)
            exporter = ArchiveBundleExporter(width=320, height=180)
            target = root / "HSR_Voice_Archive_Bundle.mkv"
            corners = {}
            for mode in ("linear", "radial", "soft"):
                spec = normalize_bundle_background({
                    "mode": mode, "direction": "horizontal",
                    "first": "#ef3344", "second": "#103351", "third": "#3855a5",
                    "resolution": "1080p", "quality": "high",
                })
                result = exporter.export(
                    audio, ass, target, require_current_ass=True,
                    background_color="#202020", background_spec=spec,
                )
                self.assertEqual(result["background"], mode)
                probe = exporter._probe(target)
                stream_map = {row["codec_type"]: row["codec_name"] for row in probe["streams"]}
                self.assertEqual(stream_map["video"], "h264")
                self.assertEqual(stream_map["audio"], "flac")
                self.assertEqual(stream_map["subtitle"], "ass")
                self.assertEqual(bundle_export_status(root)["state"], "current")
                saved = previous_bundle_settings(root)["background_spec"]
                self.assertEqual(saved, spec)
                raw = subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-i", str(target), "-frames:v", "1",
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                ], check=True, capture_output=True, timeout=30).stdout
                self.assertEqual(len(raw), 320 * 180 * 3)
                def sample(x, y):
                    i = (y * 320 + x) * 3
                    return tuple(raw[i:i + 3])
                left, right = sample(30, 90), sample(290, 90)
                self.assertGreater(sum(abs(a - b) for a, b in zip(left, right)), 12)
                corners[mode] = (left, right)
            self.assertNotEqual(corners["linear"], corners["radial"])

    def test_missing_ffmpeg_gradient_filter_does_not_destroy_previous_mkv(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio = self._audio(root)
            ass = self._ass(root)
            exporter = ArchiveBundleExporter(width=320, height=180)
            target = root / "HSR_Voice_Archive_Bundle.mkv"
            exporter.export(audio, ass, target, require_current_ass=True)
            original = target.read_bytes()
            with patch("app.archive_bundle_exporter.gradient_source",
                                    return_value=("not_a_real_ffmpeg_filter=1", None)):
                with self.assertRaises(RuntimeError):
                    exporter.export(
                        audio, ass, target, require_current_ass=True,
                        background_spec=normalize_bundle_background({"mode": "linear"}),
                    )
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(bundle_export_status(root)["state"], "current")


if __name__ == "__main__":
    unittest.main()
