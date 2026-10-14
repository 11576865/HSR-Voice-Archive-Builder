"""UI provenance, compressed reference asset and FFmpeg-backed studio preview tests."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from app.ass_preview import (
    STUDIO_REFERENCE_BACKGROUND,
    load_studio_reference_background,
    preview_config_from_payload,
    render_ass_preview_png,
)

ROOT = Path(__file__).resolve().parents[1]


class ReferenceBackgroundTests(unittest.TestCase):
    def test_compressed_image_is_real_bundled_webp(self):
        data = load_studio_reference_background()
        self.assertTrue(STUDIO_REFERENCE_BACKGROUND.is_file())
        self.assertTrue(data.startswith(b"RIFF"))
        self.assertEqual(data[8:12], b"WEBP")
        self.assertLess(len(data), 20_000)
        if shutil.which("ffprobe"):
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json", str(STUDIO_REFERENCE_BACKGROUND),
            ], capture_output=True, text=True, check=True, timeout=10)
            stream = json.loads(probe.stdout)["streams"][0]
            self.assertEqual((stream["width"], stream["height"]), (640, 360))

    def test_workbench_uses_reference_by_default_but_archive_export_does_not(self):
        html = (ROOT / "app/static/index.html").read_text("utf-8")
        css = (ROOT / "app/static/studio-v4.css").read_text("utf-8")
        self.assertIn('id="previewBackgroundImage" src="/static/assets/studio-red-clouds.webp"', html)
        self.assertIn('id="previewBackgroundMode" type="hidden" value="reference"', html)
        self.assertIn('use_studio_reference_background:', html)
        self.assertIn('payload.use_studio_reference_background=usesReference;', html)
        self.assertIn('usesReference?\'红云参考图 + ASS\'', html)
        self.assertIn("const mode='reference';", html)
        self.assertIn("invalidateLibassPreview();", html)
        self.assertIn("Final delivery settings remain independent", html)
        self.assertIn("position:relative!important", css)
        self.assertNotIn("position:sticky!important;top:0!important;z-index:9", css)
        self.assertIn(".studio-v4-timing", css)
        self.assertIn('<summary>按需调整显示时间', html)
        self.assertNotIn("studio-red-clouds.webp", (ROOT / "app/archive_bundle_exporter.py").read_text("utf-8"))
        server = (ROOT / "app/server.py").read_text("utf-8")
        lite = (ROOT / "app/lite_server.py").read_text("utf-8")
        self.assertIn('use_studio_reference_background=data.get("use_studio_reference_background") is True', server)
        self.assertIn('use_studio_reference_background=_bool(data.get("use_studio_reference_background"))', lite)
        self.assertIn('load_studio_reference_background()', server)
        self.assertIn('load_studio_reference_background()', lite)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    def test_real_libass_reference_renders_nonblack_image(self):
        cfg = preview_config_from_payload({
            "chs_font": "DejaVu Sans", "primary_font": "DejaVu Sans",
            "enable_kinetic": False,
        })
        kwargs = {
            "english_text": "Clouds reference background",
            "chinese_text": "彩云预览",
            "source_language": "en",
            "target_language": "zh-CN",
            "config": cfg,
            "duration_seconds": 2.0,
            "timestamp": 0.5,
        }
        try:
            with_ref = render_ass_preview_png(
                **kwargs, use_studio_reference_background=True,
            )
            without_ref = render_ass_preview_png(
                **kwargs, use_studio_reference_background=False,
            )
        except RuntimeError as exc:
            if "FFmpeg" in str(exc) and ("libass" in str(exc) or "No such filter" in str(exc)):
                self.skipTest("Current FFmpeg lacks libass")
            raise
        self.assertTrue(with_ref.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(with_ref), 20_000)
        self.assertNotEqual(with_ref, without_ref,
                            "Real FFmpeg output must include the selected picture, not black")
        # Explicit bare color preview remains available and is not replaced globally.
        self.assertGreater(len(without_ref), 500)


if __name__ == "__main__":
    unittest.main()
