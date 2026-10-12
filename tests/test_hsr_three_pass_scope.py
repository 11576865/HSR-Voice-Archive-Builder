"""Three-pass HSR acceptance contracts: ownership, view split, playable render."""
from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.ass_preview import render_ass_preview_video
from app.security import api_token
from app.server import app as fastapi_app
from subtitle_layout.config import SubtitleRenderConfig

ROOT = Path(__file__).resolve().parents[1]


class HsrThreePassUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")

    def test_one_user_facing_text_editor_and_audio_owner(self):
        html = self.html
        self.assertIn('id="subFinalEditor"', html)
        self.assertIn('id="layoutCueFinalText" rows="3" disabled readonly', html)
        self.assertIn('class="layout-cue-actions" hidden', html)
        self.assertIn('class="layout-cue-audio" hidden', html)
        self.assertIn('id="layoutOpenProofreadingBtn"', html)
        self.assertIn("showWorkspace('review')", html)
        self.assertIn("editor.readOnly=true", html)

    def test_style_and_effects_have_disjoint_parameter_owners(self):
        html = self.html
        self.assertIn('data-studio-mode="style"', html)
        self.assertIn('id="studioStyleTab"', html)
        self.assertIn('id="studioEffectsTab"', html)
        self.assertIn('data-studio-scope="effects"', html)
        self.assertIn('data-studio-scope="style"', html)
        self.assertIn("function setHsrStudioMode(mode)", html)

    def test_real_video_preview_not_still_image_claim(self):
        html = self.html
        self.assertIn('id="libassVideoPreview"', html)
        self.assertIn('id="previewVideoBtn"', html)
        self.assertIn("renderHsrEffectVideo()", html)
        self.assertIn("/api/subtitle-layout/render-preview-video", html)
        self.assertIn("video.addEventListener('loadeddata',ready)", html)
        self.assertIn("clearVideoPreview()", html)
        self.assertIn("黑底无声", html)

    def test_both_server_surfaces_offer_video_path(self):
        for file in ("app/server.py", "app/lite_server.py"):
            self.assertIn(
                '/api/subtitle-layout/render-preview-video',
                (ROOT / file).read_text(encoding="utf-8"),
            )

    def test_production_inline_javascript_parses_with_node(self):
        if not __import__("shutil").which("node"):
            self.skipTest("Node unavailable")
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", self.html, re.S)
        self.assertTrue(scripts)
        with tempfile.TemporaryDirectory() as td:
            for idx, script in enumerate(scripts):
                path = Path(td) / f"inline-{idx}.js"
                path.write_text(script, encoding="utf-8")
                run = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)


class HsrEffectVideoBackendTests(unittest.TestCase):
    def call_renderer(self, **override):
        kwargs = dict(
            english_text="May this journey lead us starward.",
            chinese_text="愿此行，终抵群星。",
            source_language="en",
            target_language="zh-CN",
            config=SubtitleRenderConfig(),
            duration_seconds=2.5,
        )
        kwargs.update(override)
        return render_ass_preview_video(**kwargs)

    def test_rejects_unbounded_and_nonfinite_duration_before_invoking_ffmpeg(self):
        for bad in (float("nan"), float("inf"), -1.0, 0.1, 8.01):
            with self.subTest(duration=bad):
                with self.assertRaises(ValueError):
                    self.call_renderer(duration_seconds=bad)

    def test_uses_actual_ffmpeg_subtitles_filter_and_playable_h264(self):
        def fake_run(cmd, **kwargs):
            self.assertIn("-vf", cmd)
            filt = cmd[cmd.index("-vf") + 1]
            self.assertIn("subtitles=", filt)
            self.assertIn("original_size=1920x1080", filt)
            self.assertIn("libx264", cmd)
            self.assertIn("+faststart", cmd)
            Path(cmd[-1]).write_bytes(b"fake-mp4-payload")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("app.ass_preview.shutil.which", return_value="ffmpeg"), \
                patch("app.ass_preview.write_ass") as writer, \
                patch("app.ass_preview.subprocess.run", side_effect=fake_run):
            self.assertEqual(self.call_renderer(), b"fake-mp4-payload")
            writer.assert_called_once()

    def test_authenticated_fastapi_returns_video_mimetype_not_image(self):
        client = TestClient(fastapi_app, base_url="http://127.0.0.1:8765")
        payload = {"english_text": "A", "chinese_text": "甲", "preview_duration_seconds": 2.5}
        url = "/api/subtitle-layout/render-preview-video"
        denied = client.post(url, json=payload)
        self.assertEqual(denied.status_code, 401)
        with patch("app.server.render_ass_preview_video", return_value=b"video-payload"):
            response = client.post(url, json=payload, headers={"X-HSR-Token": api_token()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertEqual(response.content, b"video-payload")


    def test_real_ffmpeg_video_can_be_decoded_and_contains_motion(self):
        import json
        import shutil

        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("Real FFmpeg / FFprobe unavailable")
        capabilities = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
            timeout=8, check=False,
        ).stdout
        filters = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
            timeout=8, check=False,
        ).stdout
        if "libx264" not in capabilities or "subtitles" not in filters:
            self.skipTest("This FFmpeg lacks H.264/libass")
        data = self.call_renderer(
            english_text="HSR effect timing test",
            chinese_text="HSR preview",
            config=SubtitleRenderConfig(
                primary_font="DejaVu Sans", chs_font="DejaVu Sans",
                fade_in_ms=500, fade_out_ms=100,
            ),
            duration_seconds=1.5,
        )
        self.assertGreater(len(data), 512)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "real.mp4"
            path.write_bytes(data)
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,nb_frames",
                 "-of", "json", str(path)],
                capture_output=True, text=True, timeout=10, check=True,
            )
            stream = json.loads(probe.stdout)["streams"][0]
            self.assertEqual(stream["codec_name"], "h264")
            self.assertGreaterEqual(int(stream["nb_frames"]), 25)

            def sample(timestamp):
                result = subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error",
                     "-ss", str(timestamp), "-i", str(path),
                     "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                    capture_output=True, timeout=15, check=True,
                )
                return result.stdout

            early = sample(0.04)
            mid = sample(0.6)
            self.assertTrue(early and mid)
            self.assertNotEqual(early, mid, "Real decoded frames did not show animated change")
