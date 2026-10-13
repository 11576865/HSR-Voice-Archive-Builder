"""Real FFmpeg+ffprobe acceptance for a selectable ASS+FLAC+font MKV artifact."""
from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from app.archive_bundle_exporter import (
    ArchiveBundleExporter, decode_bundle_background, require_current_bundle_subtitles,
)
from app.subtitle_export_status import record_subtitle_export
from app.archive_bundle_lifecycle import bundle_export_status, load_previous_bundle_image
from app import jobs as job_registry


class BundleInputValidationTests(unittest.TestCase):
    def test_mkv_routes_require_current_ass_at_launch_and_publish(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("app/server.py", "app/lite_server.py"):
            code = (root / name).read_text(encoding="utf-8")
            start = code.index('"/api/output/archive-bundle"')
            block = code[start: start + 2900]
            self.assertIn("require_current_bundle_subtitles(output)", block, name)
            self.assertIn("require_current_ass=True", block, name)

    def test_unverified_or_nonass_receipts_are_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "manifest.json").write_text('{"entries":[]}', encoding="utf-8")
            (root / "HSR_Voice_Archive.srt").write_text("one", encoding="utf-8")
            (root / "HSR_Voice_Archive.ass").write_text("[Events]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ASS 字幕未通过"):
                require_current_bundle_subtitles(root)
            record_subtitle_export(root, state="current", ass_required=False)
            with self.assertRaisesRegex(ValueError, "ASS 字幕未通过"):
                require_current_bundle_subtitles(root)
            record_subtitle_export(root, state="current", ass_required=True)
            require_current_bundle_subtitles(root)

    def test_background_is_bounded_and_sniffed(self):
        with self.assertRaisesRegex(ValueError, "PNG, JPEG or WebP"):
            decode_bundle_background(base64.b64encode(b"not an image").decode("ascii"))
        with self.assertRaises(ValueError):
            decode_bundle_background("garbage!")
        with self.assertRaises(ValueError):
            decode_bundle_background("A" * (6 * 1024 * 1024))
        self.assertIsNone(decode_bundle_background(None))


class BundleOutputJobSerializationTests(unittest.TestCase):
    def test_archive_job_blocks_other_same_project_output_writers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = str(root / "project-a")
            other = str(root / "project-b")
            started, release = threading.Event(), threading.Event()
            def hold_mux():
                started.set()
                release.wait(10)
                return {"ok": True}
            with patch.object(job_registry, "JOBS_FILE", root / "jobs.json"):
                first = job_registry.create_job(
                    "archive-bundle-export", hold_mux,
                    project_root=project, project_name="Archive A",
                )
                self.assertTrue(started.wait(timeout=8), "Archive job failed to start")
                try:
                    for kind in ("archive-bundle-export", "ass-export", "black-video-export",
                                 "quick-build", "remote-update-apply"):
                        with self.subTest(kind=kind):
                            with self.assertRaisesRegex(RuntimeError, "同一项目已有输出任务"):
                                job_registry.create_job(
                                    kind, lambda: None, project_root=project,
                                    project_name="Archive A",
                                )
                    unrelated = job_registry.create_job(
                        "archive-bundle-export", lambda: {"ok": True},
                        project_root=other, project_name="Archive B",
                    )
                    self.assertIsNotNone(job_registry.get_job(unrelated.id))
                finally:
                    release.set()
                until = time.monotonic() + 8
                while time.monotonic() < until:
                    if job_registry.get_job(first.id)["state"] == "succeeded":
                        break
                    time.sleep(0.02)
                self.assertEqual(job_registry.get_job(first.id)["state"], "succeeded")
                repeat = job_registry.create_job(
                    "archive-bundle-export", lambda: {"ok": True},
                    project_root=project, project_name="Archive A",
                )
                self.assertIsNotNone(job_registry.get_job(repeat.id))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "FFmpeg and FFprobe required for media-track evidence")
class BundleRealFFmpegTests(unittest.TestCase):
    def _audio(self, folder: Path) -> Path:
        source = folder / "continuous.flac"
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
            "-i", "sine=frequency=440:sample_rate=44100:duration=1.6",
            "-c:a", "flac", str(source)
        ], check=True, capture_output=True)
        return source

    def _ass(self, folder: Path) -> Path:
        subtitles = folder / "HSR_Voice_Archive.ass"
        subtitles.write_text(
            "[Script Info]\n"
            "ScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,DejaVu Sans,48,&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,40,40,35,1\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:01.50,Default,,0,0,0,,Test \\N soft subtitle\n",
            encoding="utf-8",
        )
        return subtitles

    @staticmethod
    def _certify(root: Path) -> None:
        (root / "manifest.json").write_text('{"entries":[]}', encoding="utf-8")
        (root / "HSR_Voice_Archive.srt").write_text(
            "1\\n00:00:00,000 --> 00:00:01,500\\nTest\\n", encoding="utf-8"
        )
        record_subtitle_export(root, state="current", ass_required=True)

    def test_stale_ass_blocks_new_mkv_and_preserves_previous_valid_archive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio, ass = self._audio(root), self._ass(root)
            self._certify(root)
            output = root / "HSR_Voice_Archive_Bundle.mkv"
            exporter = ArchiveBundleExporter(width=320, height=180)
            exporter.export(audio, ass, output, require_current_ass=True)
            previous = output.read_bytes()
            (root / "subtitle_timing_overrides.json").write_text(
                '{"modified":true}', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "ASS 字幕未通过"):
                exporter.export(audio, ass, output, require_current_ass=True)
            self.assertEqual(output.read_bytes(), previous)

    def test_ass_mutation_during_mux_fails_before_atomic_replace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio, ass = self._audio(root), self._ass(root)
            self._certify(root)
            output = root / "HSR_Voice_Archive_Bundle.mkv"
            exporter = ArchiveBundleExporter(width=320, height=180)
            exporter.export(audio, ass, output, require_current_ass=True)
            previous = output.read_bytes()
            original_validate = exporter.validate
            def mutate_after_validation(*args, **kwargs):
                original_validate(*args, **kwargs)
                (root / "subtitles_overrides.json").write_text(
                    '{"edit":"during mux"}', encoding="utf-8"
                )
            exporter.validate = mutate_after_validation
            with self.assertRaisesRegex(ValueError, "ASS 字幕未通过"):
                exporter.export(audio, ass, output, require_current_ass=True,
                                background_color="#112233")
            self.assertEqual(output.read_bytes(), previous)

    def test_wrong_video_codec_is_rejected_even_with_valid_flac_and_ass(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio, ass = self._audio(root), self._ass(root)
            exporter = ArchiveBundleExporter(width=320, height=180)
            valid = root / "valid.mkv"
            invalid = root / "wrong-video.mkv"
            exporter.export(audio, ass, valid)
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(valid),
                "-map", "0", "-c:v", "mpeg4", "-q:v", "5",
                "-c:a", "copy", "-c:s", "copy", str(invalid),
            ], check=True, capture_output=True, timeout=40)
            with self.assertRaisesRegex(RuntimeError, "H.264"):
                exporter.validate(invalid, expected_fonts=0, expected_duration=1.6)

    def test_symlinked_font_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio, ass = self._audio(root), self._ass(root)
            actual = root / "redirected"
            actual.mkdir()
            link = root / "subtitle_fonts"
            try:
                link.symlink_to(actual, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"Symlinks unavailable: {exc}")
            exporter = ArchiveBundleExporter(width=320, height=180)
            with self.assertRaisesRegex(ValueError, "符号链接"):
                exporter.export(audio, ass, root / "out.mkv",
                                fonts_dir=link, include_fonts=True)
            self.assertFalse((root / "out.mkv").exists())

    def test_real_soft_ass_audio_video_with_optional_font_attachment(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio, ass = self._audio(root), self._ass(root)
            fonts = root / "subtitle_fonts"
            fonts.mkdir()
            family_font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
            if family_font.is_file():
                shutil.copyfile(family_font, fonts / "DejaVuSans.ttf")
            target = root / "HSR_Voice_Archive_Bundle.mkv"
            exporter = ArchiveBundleExporter(width=320, height=180)
            result = exporter.export(audio, ass, target, fonts_dir=fonts)
            self.assertTrue(target.is_file())
            self.assertTrue(result["subtitles_embedded"])
            self.assertFalse(result["subtitles_burned_in"])
            self.assertEqual(result["audio_codec"], "flac")
            self.assertEqual(result["font_attachments"], int(family_font.is_file()))
            probe = exporter._probe(target)
            codecs = {stream.get("codec_type"): stream.get("codec_name")
                      for stream in probe["streams"]}
            self.assertEqual(codecs["audio"], "flac")
            self.assertEqual(codecs["subtitle"], "ass")
            self.assertEqual(codecs["video"], "h264")
            self.assertAlmostEqual(float(probe["format"]["duration"]), 1.6, delta=0.5)
            if family_font.is_file():
                with self.assertRaisesRegex(RuntimeError, "名称与实际项目字体不一致"):
                    exporter.validate(
                        target, expected_fonts=1, expected_duration=1.6,
                        expected_font_names={"different.ttf"},
                    )

    def test_saved_background_is_reused_after_new_flac_and_mkv_atomic_replace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio, ass = self._audio(root), self._ass(root)
            self._certify(root)
            image = root / "background.png"
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=c=red:s=160x90:d=1", "-frames:v", "1", str(image)
            ], check=True, capture_output=True)
            original_image = image.read_bytes()
            exporter = ArchiveBundleExporter(width=320, height=180)
            output = root / "HSR_Voice_Archive_Bundle.mkv"
            exporter.export(
                audio, ass, output, require_current_ass=True,
                background_image=original_image, background_color="#162431",
            )
            self.assertEqual(bundle_export_status(root)["state"], "current")
            self.assertEqual(load_previous_bundle_image(root), original_image)
            previous_bytes = output.read_bytes()
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                "-i", "sine=frequency=630:sample_rate=44100:duration=2.2",
                "-c:a", "flac", str(audio)
            ], check=True, capture_output=True)
            self.assertEqual(bundle_export_status(root)["state"], "stale")
            self.assertEqual(previous_bytes, output.read_bytes())
            exporter.export(
                audio, ass, output, require_current_ass=True,
                background_image=load_previous_bundle_image(root),
                background_color="#162431",
            )
            self.assertEqual(bundle_export_status(root)["state"], "current")
            self.assertNotEqual(previous_bytes, output.read_bytes())
            self.assertEqual(load_previous_bundle_image(root), original_image)
            probe = exporter._probe(output)
            self.assertAlmostEqual(float(probe["format"]["duration"]), 2.2, delta=0.5)
            frame = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(output),
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            ], capture_output=True, check=True, timeout=25).stdout
            center = (90 * 320 + 160) * 3
            self.assertGreater(frame[center], frame[center + 1] + 35)

    def test_background_picture_is_actual_mkv_video(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio, ass = self._audio(root), self._ass(root)
            image = root / "background.png"
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=c=red:s=160x90:d=1", "-frames:v", "1", str(image),
            ], check=True, capture_output=True)
            exporter = ArchiveBundleExporter(width=320, height=180)
            target = root / "picture.mkv"
            result = exporter.export(audio, ass, target,
                                     background_color="#162431",
                                     background_image=image.read_bytes())
            self.assertEqual(result["background"], "image")
            # Decoding a real frame ensures image-background generation is not
            # merely a stored image filename or a CSS background.
            frame = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", str(target), "-frames:v", "1", "-f", "rawvideo",
                "-pix_fmt", "rgb24", "pipe:1",
            ], capture_output=True, check=True, timeout=25).stdout
            self.assertEqual(len(frame), 320 * 180 * 3)
            center = (90 * 320 + 160) * 3
            self.assertGreater(frame[center], frame[center + 1] + 35)


if __name__ == "__main__":
    unittest.main()
