"""Real PNG/JPEG -> FFmpeg frames -> ASS video and API provenance proof."""
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

from app.ass_preview import (
    MAX_PREVIEW_IMAGE_BYTES, decode_preview_background_image, render_ass_preview_video,
)
from app.security import api_token
from app.server import app
from subtitle_layout.config import SubtitleRenderConfig


class BackgroundImageTests(unittest.TestCase):
    def test_decoder_enforces_image_magic_size_and_format(self):
        self.assertIsNone(decode_preview_background_image(None))
        self.assertIsNone(decode_preview_background_image(""))
        for data in (b"not-an-image", b"<svg/>", b"RIFF" + b"A" * 20,
                     b"GIF89a" + b"A" * 30):
            with self.subTest(data=data[:8]), self.assertRaises(ValueError):
                decode_preview_background_image(base64.b64encode(data).decode())
        for value in ("invalid-%%%!", "/local/path.png", 123, [],
                      base64.b64encode(b"\\x89PNG\\r\\n\\x1a\\n" + b"A" * MAX_PREVIEW_IMAGE_BYTES).decode()):
            with self.subTest(value=str(value)[:32]), self.assertRaises(ValueError):
                decode_preview_background_image(value)

    def test_fastapi_provenance_and_image_video_exclusion(self):
        png_bytes = b"\\x89PNG\\r\\n\\x1a\\n" + b"dummy"
        clip = TestClient(app, base_url="http://127.0.0.1:8765")
        body = {"english_text": "", "chinese_text": "",
                "preview_background_image_base64": base64.b64encode(png_bytes).decode()}
        with patch("app.server.render_ass_preview_video", return_value=b"generated") as renderer:
            response = clip.post("/api/subtitle-layout/render-preview-video",
                                 json=body, headers={"X-HSR-Token": api_token()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-hsr-preview-background"], "local-image")
        self.assertEqual(renderer.call_args.kwargs["background_image_bytes"], png_bytes)
        with patch("app.server.render_ass_preview_video") as renderer:
            response = clip.post("/api/subtitle-layout/render-preview-video",
                                 json={**body, "preview_background_image_base64": "bad"},
                                 headers={"X-HSR-Token": api_token()})
        self.assertEqual(response.status_code, 400)
        renderer.assert_not_called()
        with self.assertRaises(ValueError):
            render_ass_preview_video(
                english_text="", chinese_text="", source_language="en",
                target_language="zh-CN", config=SubtitleRenderConfig(),
                source_video_bytes=b"video", background_image_bytes=png_bytes
            )

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "Actual FFmpeg/FFprobe required")
    def test_actual_image_pixel_survives_ffmpeg_and_libass(self):
        ffmpeg = shutil.which("ffmpeg")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "magenta.png"
            subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=c=0xef2299:s=320x180",
                 "-frames:v", "1", str(image)],
                capture_output=True, check=True, timeout=25,
            )
            png = image.read_bytes()
            self.assertEqual(decode_preview_background_image(
                base64.b64encode(png).decode()), png)
            video_bytes = render_ass_preview_video(
                english_text="Hello", chinese_text="图片背景实际渲染",
                source_language="en", target_language="zh-CN",
                config=SubtitleRenderConfig(),
                duration_seconds=0.65, container_format="mp4",
                background_image_bytes=png, background_color="#001122",
            )
            out = root / "image-background.mp4"
            out.write_bytes(video_bytes)
            details = subprocess.run(
                [shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,codec_name",
                 "-of", "json", str(out)],
                capture_output=True, text=True, check=True, timeout=15,
            )
            stream = json.loads(details.stdout)["streams"][0]
            self.assertEqual((stream["width"], stream["height"]), (960, 540))
            pixel = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(out),
                 "-vf", "crop=2:2:20:20,format=rgb24",
                 "-frames:v", "1", "-f", "rawvideo", "-"],
                capture_output=True, check=True, timeout=25,
            ).stdout
            self.assertEqual(len(pixel), 12)
            for actual, expected in zip(pixel[:3], (0xef, 0x22, 0x99)):
                self.assertLessEqual(abs(actual - expected), 24, list(pixel[:3]))

    def test_frontend_image_controls_reach_real_preview_payload(self):
        html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text("utf-8")
        for required in (
            'id="effectBackgroundImageFile"',
            'id="effectBackgroundImageClearBtn"',
            'payload.preview_background_image_base64=await effectBackgroundImageAsBase64(backgroundImage)',
            "actualBackground && actualBackground!==",
            "'local-image'",
            "setPreviewEvidence(footage?'video-footage':backgroundImage?'video-image':'video')",
        ):
            self.assertIn(required, html)


if __name__ == "__main__":
    unittest.main()
