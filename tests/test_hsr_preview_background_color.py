"""FFmpeg image-backed proof and API contract for custom dynamic-video background."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.ass_preview import normalize_preview_background_color, render_ass_preview_video
from app.security import api_token
from app.server import app
from subtitle_layout.config import SubtitleRenderConfig


class BackgroundColorTests(unittest.TestCase):
    def test_strict_validated_hex_color(self):
        self.assertEqual(normalize_preview_background_color(None), "#000000")
        self.assertEqual(normalize_preview_background_color(""), "#000000")
        self.assertEqual(normalize_preview_background_color("#A32B50"), "#a32b50")
        for bad in ("red", "#ff0", "#123456:input=evil", "0x123456", "ffffff",
                    None if False else 42, "\\n", "#fff000\\n", [], "#abcdex"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                normalize_preview_background_color(bad)

    def test_fastapi_color_header_and_source_priority(self):
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        uri = "/api/subtitle-layout/render-preview-video"
        body = {"english_text": "", "chinese_text": "", "preview_background_color": "#234567"}
        with patch("app.server.render_ass_preview_video", return_value=b"real-renderer") as renderer:
            response = client.post(uri, json=body, headers={"X-HSR-Token": api_token()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-hsr-preview-background"], "solid-color")
        self.assertEqual(renderer.call_args.kwargs["background_color"], "#234567")
        with patch("app.server.render_ass_preview_video") as renderer:
            response = client.post(uri, json={**body, "preview_background_color": "bad:expression"},
                                   headers={"X-HSR-Token": api_token()})
        self.assertEqual(response.status_code, 400)
        renderer.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg"), "Actual FFmpeg required")
    def test_genuine_pixel_color_in_encoded_video(self):
        ffmpeg = shutil.which("ffmpeg")
        clip = render_ass_preview_video(
            english_text="", chinese_text="", source_language="en", target_language="zh-CN",
            config=SubtitleRenderConfig(), duration_seconds=0.6,
            container_format="mp4", background_color="#234567")
        with tempfile.TemporaryDirectory() as td:
            video = Path(td) / "color.mp4"
            video.write_bytes(clip)
            proc = subprocess.run([
                ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video),
                "-vf", "crop=1:1:0:0,format=rgb24", "-frames:v", "1",
                "-f", "rawvideo", "-"
            ], capture_output=True, check=True, timeout=25)
        self.assertEqual(len(proc.stdout), 3, "Decoded RGB frame required")
        rgb = proc.stdout
        for channel, expected in zip(rgb, (0x23, 0x45, 0x67)):
            self.assertLessEqual(abs(channel - expected), 18, (list(rgb), expected))

    def test_frontend_background_wiring(self):
        html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text("utf-8")
        self.assertIn('id="effectPreviewBackgroundColor"', html)
        self.assertIn("payload.preview_background_color=backgroundColor", html)
        self.assertIn("backgroundColor===(document.getElementById('effectPreviewBackgroundColor')", html)
        self.assertIn("actualBackground && actualBackground!==", html)
        self.assertIn("addEventListener('input',()=>{", html)


if __name__ == "__main__":
    unittest.main()
