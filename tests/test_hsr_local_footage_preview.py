"""Opt-in local media composes through the same actual libass rendering route."""
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
    MAX_PREVIEW_SOURCE_BYTES,
    _validate_preview_source,
    decode_preview_source_video,
    render_ass_preview_video,
)
from app.security import api_token
from app.server import app
from subtitle_layout.config import SubtitleRenderConfig


def source_video_fixture(ffmpeg: str, target: Path) -> bytes:
    subprocess.run([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=2.5",
        "-an", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", str(target),
    ], check=True, capture_output=True, timeout=25)
    return target.read_bytes()


class LocalFootageContractTests(unittest.TestCase):
    def call(self, **overrides):
        opts = {
            "english_text": "The stars are still beyond our reach.",
            "chinese_text": "星辰大海。",
            "source_language": "en", "target_language": "zh-CN",
            "config": SubtitleRenderConfig(),
            "duration_seconds": 1.5,
        }
        opts.update(overrides)
        return render_ass_preview_video(**opts)

    def test_inline_decoder_blocks_oversized_and_non_base64_inputs(self):
        self.assertIsNone(decode_preview_source_video(None))
        self.assertIsNone(decode_preview_source_video(""))
        self.assertEqual(decode_preview_source_video(base64.b64encode(b"abc").decode()), b"abc")
        for value in ("relative/video.mp4", "https://example.com/media.mp4", "***",
                      base64.b64encode(b"A" * (MAX_PREVIEW_SOURCE_BYTES + 1)).decode(),
                      12, []):
            with self.subTest(value=str(value)[:60]), self.assertRaises(ValueError):
                decode_preview_source_video(value)

    def test_source_probe_rejects_large_dimensions_and_too_short_window(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "video.bin"
            source.write_bytes(b"data")
            for width, duration in [(4000, 60), (320, 1)]:
                fake = subprocess.CompletedProcess(
                    [], 0, json.dumps({
                        "streams": [{"codec_type": "video", "width": width, "height": 180}],
                        "format": {"duration": duration},
                    }), "")
                with patch("app.ass_preview.shutil.which", return_value="ffprobe"), \
                     patch("app.ass_preview.subprocess.run", return_value=fake):
                    with self.assertRaises(ValueError):
                        _validate_preview_source(source, 0.0, 1.5)

    def test_ffmpeg_reads_local_source_not_generated_black(self):
        source_bytes = b"fictional-media-input"
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "ffprobe" in cmd[0]:
                return subprocess.CompletedProcess(
                    cmd, 0, json.dumps({
                        "streams": [{"codec_type": "video", "width": 320, "height": 180}],
                        "format": {"duration": 5},
                    }), "")
            self.assertIn("-ss", cmd)
            self.assertEqual(cmd[cmd.index("-ss") + 1], "1.000")
            self.assertNotIn("lavfi", cmd)
            source_path = Path(cmd[cmd.index("-i") + 1])
            self.assertEqual(source_path.read_bytes(), source_bytes)
            filt = cmd[cmd.index("-vf") + 1]
            self.assertIn("force_original_aspect_ratio=decrease", filt)
            self.assertIn("subtitles=", filt)
            Path(cmd[-1]).write_bytes(b"coded-with-subtitles")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("app.ass_preview.shutil.which", side_effect=lambda n: n), \
             patch("app.ass_preview.write_ass"), \
             patch("app.ass_preview.subprocess.run", side_effect=fake_run):
            video = self.call(source_video_bytes=source_bytes, source_start_seconds=1)
        self.assertEqual(video, b"coded-with-subtitles")
        self.assertEqual(len(calls), 2)
        self.assertFalse(Path(calls[1][-1]).exists(), "Temp source/output should not persist")

    def test_invalid_source_start_fails_without_ffmpeg(self):
        for start in (-1, 601, float("nan"), float("inf")):
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.call(source_start_seconds=start)

    def test_fastapi_checks_token_and_preserves_actual_background_identity(self):
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        url = "/api/subtitle-layout/render-preview-video"
        data = {"english_text": "Hello", "chinese_text": "你好",
                "source_video_base64": base64.b64encode(b"fake-movie").decode(),
                "source_start_seconds": 1.2, "preview_format": "webm"}
        self.assertEqual(client.post(url, json=data).status_code, 401)
        with patch("app.server.render_ass_preview_video", return_value=b"video") as renderer:
            res = client.post(url, json=data, headers={"X-HSR-Token": api_token()})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.headers["content-type"], "video/webm")
        self.assertEqual(res.headers["x-hsr-preview-background"], "local-footage")
        self.assertEqual(renderer.call_args.kwargs["source_video_bytes"], b"fake-movie")
        self.assertEqual(renderer.call_args.kwargs["source_start_seconds"], 1.2)
        self.assertIs(renderer.call_args.kwargs["include_source_audio"], True)
        with patch("app.server.render_ass_preview_video") as renderer:
            bad = client.post(url, json={**data, "source_video_base64": "not-base64"},
                              headers={"X-HSR-Token": api_token()})
        self.assertEqual(bad.status_code, 400)
        renderer.assert_not_called()

    def test_bounded_input_accepts_medium_clip_and_rejects_over_limit(self):
        medium = b"x" * (5 * 1024 * 1024)
        self.assertEqual(decode_preview_source_video(base64.b64encode(medium).decode()), medium)
        with self.assertRaises(ValueError):
            decode_preview_source_video(base64.b64encode(
                b"x" * (MAX_PREVIEW_SOURCE_BYTES + 1)
            ).decode())

    def test_audio_options_explicitly_follow_selected_source(self):
        source_bytes = b"fake-source"
        for fmt, audio, expected in [
            ("mp4", True, "aac"),
            ("webm", True, "libopus"),
            ("mp4", False, None),
        ]:
            with self.subTest(fmt=fmt, audio=audio):
                def fake_run(cmd, **kwargs):
                    if "ffprobe" in cmd[0]:
                        return subprocess.CompletedProcess(cmd, 0, json.dumps({
                            "streams": [{"codec_type": "video", "width": 320, "height": 180}],
                            "format": {"duration": 7.0},
                        }), "")
                    self.assertIn("-map", cmd)
                    self.assertIn("0:v:0", cmd)
                    if audio:
                        self.assertIn("0:a:0?", cmd)
                        self.assertIn(expected, cmd)
                        self.assertNotIn("-an", cmd)
                    else:
                        self.assertIn("-an", cmd)
                        self.assertNotIn("0:a:0?", cmd)
                    Path(cmd[-1]).write_bytes(b"encoded")
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                with patch("app.ass_preview.shutil.which", side_effect=lambda n: n), \
                     patch("app.ass_preview.write_ass"), \
                     patch("app.ass_preview.subprocess.run", side_effect=fake_run):
                    result = render_ass_preview_video(
                        english_text="Hello", chinese_text="你好",
                        source_language="en", target_language="zh-CN",
                        config=SubtitleRenderConfig(), duration_seconds=2,
                        source_video_bytes=source_bytes, container_format=fmt,
                        include_source_audio=audio,
                    )
                self.assertEqual(result, b"encoded")


    def test_static_image_ffmpeg_path_and_no_source_audio(self):
        png = b"\x89PNG\r\n\x1a\n" + b"mock-image"
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "ffprobe":
                return subprocess.CompletedProcess(cmd, 0, json.dumps({
                    "streams": [{"codec_type": "video", "width": 640, "height": 360}]
                }), "")
            self.assertIn("-loop", cmd)
            self.assertEqual(cmd[cmd.index("-loop") + 1], "1")
            self.assertIn("-framerate", cmd)
            self.assertIn("-an", cmd)
            self.assertNotIn("0:a:0?", cmd)
            self.assertIn("subtitles=", cmd[cmd.index("-vf") + 1])
            self.assertEqual(Path(cmd[cmd.index("-i") + 1]).read_bytes(), png)
            Path(cmd[-1]).write_bytes(b"image-source-video-encoded")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch("app.ass_preview.shutil.which", side_effect=lambda n: n), \
             patch("app.ass_preview.write_ass"), \
             patch("app.ass_preview.subprocess.run", side_effect=fake_run):
            encoded = self.call(background_image_bytes=png, background_color="#2468ac")
        self.assertEqual(encoded, b"image-source-video-encoded")
        self.assertEqual(len(calls), 2)
        for options in [
            {"background_image_bytes": png, "source_video_bytes": b"clip"},
            {"background_image_bytes": b"not-an-image"},
            {"background_image_bytes": png + b"x" * (4 * 1024 * 1024)},
        ]:
            with self.subTest(options=str(options.keys())), self.assertRaises(ValueError):
                self.call(**options)

    def test_api_accepts_verified_background_image_identity(self):
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        png = b"\x89PNG\r\n\x1a\n" + b"mock-image"
        data = {"chinese_text": "你好", "english_text": "Hi",
                "preview_background_image_base64": base64.b64encode(png).decode(),
                "preview_background_color": "#2468ac", "preview_format": "webm"}
        with patch("app.server.render_ass_preview_video", return_value=b"video") as render:
            response = client.post("/api/subtitle-layout/render-preview-video",
                                   json=data, headers={"X-HSR-Token": api_token()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-hsr-preview-background"], "background-image")
        self.assertEqual(render.call_args.kwargs["background_image_bytes"], png)
        invalid = client.post("/api/subtitle-layout/render-preview-video",
                              json={**data, "preview_background_image_base64":
                                    base64.b64encode(b"bad-file").decode()},
                              headers={"X-HSR-Token": api_token()})
        self.assertEqual(invalid.status_code, 400)

    def test_ui_selected_video_is_explicit_and_not_persisted(self):
        html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text("utf-8")
        for literal in ('id="effectFootageFile"', 'id="effectFootageStart"',
                        'id="effectFootageClearBtn"', "effectFootageAsBase64",
                        "payload.source_video_base64", "payload.source_start_seconds",
                        "X-HSR-Preview-Background", "'video-footage'",
                        'id="effectFootageAudio"', "payload.include_source_audio",
                        'id="effectBackgroundImageFile"', "payload.preview_background_image_base64"):
            self.assertIn(literal, html)


class RealSourceFootageProof(unittest.TestCase):
    def test_real_background_image_produces_video_frames(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg/FFprobe unavailable")
        encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=8).stdout
        filters = subprocess.run([ffmpeg, "-hide_banner", "-filters"],
                                capture_output=True, text=True, timeout=8).stdout
        if "libx264" not in encoders or "subtitles" not in filters:
            self.skipTest("libx264/libass unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            picture = root / "background.png"
            subprocess.run([
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=1",
                "-frames:v", "1", "-threads", "1", "-update", "1",
                str(picture)
            ], capture_output=True, check=True, timeout=20)
            encoded = render_ass_preview_video(
                english_text="Actual picture", chinese_text="真实背景",
                source_language="en", target_language="zh-CN",
                config=SubtitleRenderConfig(
                    chs_font="DejaVu Sans", primary_font="DejaVu Sans",
                    fade_in_ms=0, fade_out_ms=0, enable_kinetic=False,
                ), background_color="#112233",
                background_image_bytes=picture.read_bytes(),
                container_format="mp4", duration_seconds=1.0,
            )
            target = root / "preview.mp4"
            target.write_bytes(encoded)
            probe = subprocess.run([
                ffprobe, "-v", "error", "-show_entries",
                "stream=codec_type,width,height:format=duration",
                "-of", "json", str(target)
            ], capture_output=True, text=True, check=True, timeout=10)
            result = json.loads(probe.stdout)
            video = next(s for s in result["streams"] if s["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (960, 540))
            self.assertGreater(float(result["format"]["duration"]), 0.8)
            frame = subprocess.run([
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-ss", "0.5", "-i", str(target), "-frames:v", "1",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"
            ], capture_output=True, check=True, timeout=15).stdout
            self.assertEqual(len(frame), 960*540*3)
            self.assertGreater(len(set(frame)), 30,
                               "Actual image background must preserve a colorful input, not pure color")

    def test_real_ffmpeg_video_is_overlaid_with_ass(self):
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg/FFprobe not installed")
        caps = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                              text=True, timeout=8).stdout
        filters = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True,
                                 text=True, timeout=8).stdout
        if "libx264" not in caps or "subtitles" not in filters:
            self.skipTest("libx264/libass unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_bytes = source_video_fixture(ffmpeg, root / "input.mp4")
            self.assertLessEqual(len(input_bytes), MAX_PREVIEW_SOURCE_BYTES)
            opts = {
                "source_video_bytes": input_bytes, "source_start_seconds": 0.2,
                "source_language": "en", "target_language": "zh-CN",
                "duration_seconds": 1.5,
                "container_format": "mp4",
                "config": SubtitleRenderConfig(
                    chs_font="DejaVu Sans", primary_font="DejaVu Sans",
                    fade_in_ms=0, fade_out_ms=0, enable_kinetic=False,
                ),
            }
            overlay = render_ass_preview_video(
                english_text="Subtitle visible", chinese_text="HSR", **opts)
            # The ASS writer properly rejects all-empty dialogue: build a
            # visually identical clean baseline with the same FFmpeg settings.
            clean_path = root / "clean.mp4"
            subprocess.run([
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", "0.200", "-i", str(root / "input.mp4"),
                "-vf", ("scale=960:540:force_original_aspect_ratio=decrease,"
                        "pad=960:540:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,"
                        "format=yuv420p"),
                "-an", "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "22", "-movflags", "+faststart",
                "-t", "1.500", str(clean_path),
            ], check=True, capture_output=True, timeout=35)
            clean = clean_path.read_bytes()
            self.assertGreater(len(overlay), 1000)
            for name, encoded in (("overlay", overlay), ("clean", clean)):
                target = root / (name + ".mp4")
                target.write_bytes(encoded)
                probe = subprocess.run([
                    ffprobe, "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name,width,height",
                    "-of", "json", str(target),
                ], check=True, capture_output=True, text=True, timeout=10)
                stream = json.loads(probe.stdout)["streams"][0]
                self.assertEqual(stream["codec_name"], "h264")
                self.assertEqual((stream["width"], stream["height"]), (960, 540))

            def decode_frame(name: str) -> bytes:
                return subprocess.run([
                    ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-ss", "0.7", "-i", str(root / (name + ".mp4")),
                    "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
                ], check=True, capture_output=True, timeout=15).stdout

            actual, control = decode_frame("overlay"), decode_frame("clean")
            self.assertEqual(len(actual), 960 * 540 * 3)
            self.assertEqual(len(actual), len(control))
            differences = sum(a != b for a, b in zip(actual, control))
            self.assertGreater(differences, 1000,
                               "ASS text should change real video pixels over the source footage")
            self.assertTrue(any(v for v in control), "Selected source should not be black")
