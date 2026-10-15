"""Verify PNG/JPEG/WebP background provenance and actual decoded ASS preview pixels."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.archive_bundle_exporter import MAX_BACKGROUND_BYTES, decode_bundle_background
from app.ass_preview import _validate_preview_background_image, render_ass_preview_video
from app.security import api_token
from app.server import app
from subtitle_layout.config import SubtitleRenderConfig


class DynamicBackgroundImageTests(unittest.TestCase):
    def test_decoder_rejects_oversize_and_invalid_input(self):
        self.assertIsNone(decode_bundle_background(None))
        with self.assertRaises(ValueError):
            decode_bundle_background(base64.b64encode(b"not a PNG").decode())
        with self.assertRaises(ValueError):
            decode_bundle_background("invalid-%%")
        with self.assertRaises(ValueError):
            decode_bundle_background(base64.b64encode(
                b"\x89PNG\r\n\x1a\n" + b"x" * MAX_BACKGROUND_BYTES
            ).decode())

    def test_fastapi_image_provenance_and_invalid_image(self):
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        image = b"\x89PNG\r\n\x1a\n" + b"ffprobe-validation-happens-in-renderer"
        data = {"english_text": "", "chinese_text": "",
                "preview_background_image_base64": base64.b64encode(image).decode()}
        with patch("app.server.render_ass_preview_video", return_value=b"rendered-video") as render:
            result = client.post(
                "/api/subtitle-layout/render-preview-video", json=data,
                headers={"X-HSR-Token": api_token()},
            )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.headers["x-hsr-preview-background"], "background-image")
        self.assertEqual(render.call_args.kwargs["background_image_bytes"], image)
        with patch("app.server.render_ass_preview_video") as render:
            result = client.post(
                "/api/subtitle-layout/render-preview-video",
                json={**data, "preview_background_image_base64": "invalid"},
                headers={"X-HSR-Token": api_token()},
            )
        self.assertEqual(result.status_code, 400)
        render.assert_not_called()

    def test_common_phone_photo_and_pixel_safety_gate(self):
        # FFprobe provides the source dimensions; the final preview always
        # downsizes to 960×540. Do not reject a compressed 12 MP phone image
        # merely because its width is greater than the old 3840-pixel cap.
        for width, height, accepted in (
            (4032, 3024, True),
            (6000, 4000, True),  # Exactly 24 megapixels.
            (8192, 2929, True),
            (8192, 3000, False),
            (9000, 100, False),
            (16, 16, True),
            (15, 100, False),
        ):
            with self.subTest(width=width, height=height):
                probe = subprocess.CompletedProcess(
                    ["ffprobe"], 0,
                    stdout=json.dumps({"streams": [{
                        "codec_type": "video", "width": width, "height": height,
                    }]}),
                )
                with (patch("app.ass_preview.shutil.which", return_value="ffprobe"),
                      patch("app.ass_preview.subprocess.run", return_value=probe)):
                    if accepted:
                        _validate_preview_background_image(Path("sample.jpg"))
                    else:
                        with self.assertRaisesRegex(ValueError, "safe limit"):
                            _validate_preview_background_image(Path("sample.jpg"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "Real FFmpeg/FFprobe required")
    def test_real_png_pixel_survives_dynamic_libass_video(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "magenta.png"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=0xef2299:s=320x180",
                "-frames:v", "1", str(source),
            ], capture_output=True, check=True, timeout=25)
            png = source.read_bytes()
            self.assertEqual(decode_bundle_background(base64.b64encode(png).decode()), png)
            video = render_ass_preview_video(
                english_text="Hello", chinese_text="图片背景",
                source_language="en", target_language="zh-CN",
                config=SubtitleRenderConfig(),
                duration_seconds=0.65, container_format="mp4",
                background_image_bytes=png, background_color="#001122",
            )
            output = root / "preview.mp4"
            output.write_bytes(video)
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,codec_name",
                "-of", "json", str(output),
            ], capture_output=True, text=True, check=True, timeout=15)
            stream = json.loads(probe.stdout)["streams"][0]
            self.assertEqual((stream["width"], stream["height"]), (960, 540))
            decoded = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(output),
                "-vf", "crop=2:2:20:20,format=rgb24",
                "-frames:v", "1", "-f", "rawvideo", "-",
            ], capture_output=True, check=True, timeout=25).stdout
            self.assertEqual(len(decoded), 12)
            for actual, expected in zip(decoded[:3], (0xef, 0x22, 0x99)):
                self.assertLessEqual(abs(actual - expected), 24, list(decoded[:3]))

    def test_frontend_preview_has_one_reference_background(self):
        html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text("utf-8")
        self.assertIn('use_studio_reference_background:true', html)
        self.assertNotIn('id="effectBackgroundImageFile"', html)
        self.assertNotIn('id="effectPreviewBackgroundColor"', html)
        self.assertIn('id="effectFootageFile"', html)
        # The backend can render supplied images for compatible clients,
        # but the studio no longer exposes multiple overlapping backdrops.


if __name__ == "__main__":
    unittest.main()
