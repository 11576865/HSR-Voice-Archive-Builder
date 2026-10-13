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
            # Match the *route handler*, not an earlier unrelated budget
            # table that happens to contain the same endpoint string.
            marker = ('if path == "/api/output/archive-bundle":'
                      if name.endswith("lite_server.py")
                      else '@app.post("/api/output/archive-bundle")')
            start = code.index(marker)
            block = code[start: start + 4200]
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

    def test_default_dual_soft_subtitle_tracks_and_srt_staleness(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio, ass = self._audio(root), self._ass(root)
            self._certify(root)
            target = root / "HSR_Voice_Archive_Bundle.mkv"
            exporter = ArchiveBundleExporter(width=320, height=180)
            result = exporter.export(audio, ass, target, require_current_ass=True)
            self.assertEqual(result["subtitle_codecs"], ["ass", "subrip"])
            streams = exporter._probe(target)["streams"]
            self.assertEqual([row["codec_name"] for row in streams if row["codec_type"] == "subtitle"],
                             ["ass", "subrip"])
            self.assertEqual(len([row for row in streams if row["codec_type"] == "audio"]), 1)
            self.assertEqual(bundle_export_status(root)["state"], "current")
            previous = target.read_bytes()
            (root / "HSR_Voice_Archive.srt").write_text(
                "1\\n00:00:00,000 --> 00:00:01,500\\nExternally edited\\n", encoding="utf-8")
            self.assertEqual(bundle_export_status(root)["state"], "stale")
            self.assertEqual(target.read_bytes(), previous)
            with self.assertRaises(ValueError):
                exporter.export(audio, ass, target, require_current_ass=True)
            self.assertEqual(target.read_bytes(), previous)

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

    def test_external_flac_replacement_during_mux_preserves_prior_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio, ass = self._audio(root), self._ass(root)
            self._certify(root)
            output = root / "HSR_Voice_Archive_Bundle.mkv"
            exporter = ArchiveBundleExporter(width=320, height=180)
            exporter.export(audio, ass, output, require_current_ass=True)
            from app.archive_bundle_lifecycle import RECEIPT
            previous_mkv = output.read_bytes()
            previous_receipt = (root / RECEIPT).read_bytes()
            original_validate = exporter.validate

            def replace_audio_after_validation(*args, **kwargs):
                original_validate(*args, **kwargs)
                replacement = root / "replacement.flac"
                subprocess.run([
                    "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=650:sample_rate=44100:duration=1.6",
                    "-c:a", "flac", str(replacement),
                ], check=True, capture_output=True, timeout=30)
                replacement.replace(audio)

            exporter.validate = replace_audio_after_validation
            with self.assertRaisesRegex(RuntimeError, "源文件在封装期间发生变化"):
                exporter.export(audio, ass, output, require_current_ass=True)
            self.assertEqual(output.read_bytes(), previous_mkv)
            self.assertEqual((root / RECEIPT).read_bytes(), previous_receipt)
            self.assertEqual(bundle_export_status(root)["state"], "stale")

    def test_external_ass_edit_during_unverified_mux_keeps_previous_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio, ass = self._audio(root), self._ass(root)
            output = root / "uncertified.mkv"
            exporter = ArchiveBundleExporter(width=320, height=180)
            exporter.export(audio, ass, output)
            previous_mkv = output.read_bytes()
            original_validate = exporter.validate

            def edit_ass_after_validation(*args, **kwargs):
                original_validate(*args, **kwargs)
                with ass.open("a", encoding="utf-8") as handle:
                    handle.write("; Changed while FFmpeg was muxing\n")

            exporter.validate = edit_ass_after_validation
            with self.assertRaisesRegex(RuntimeError, "源文件在封装期间发生变化"):
                exporter.export(audio, ass, output)
            self.assertEqual(output.read_bytes(), previous_mkv)

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

    def test_failed_publication_restores_previous_mkv_and_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio, ass = self._audio(root), self._ass(root)
            self._certify(root)
            exporter = ArchiveBundleExporter(width=320, height=180)
            output = root / "HSR_Voice_Archive_Bundle.mkv"
            exporter.export(audio, ass, output, require_current_ass=True)
            from app.archive_bundle_lifecycle import RECEIPT
            previous_mkv = output.read_bytes()
            previous_receipt = (root / RECEIPT).read_bytes()
            replace = Path.replace
            def deny_publishing(self, target):
                if self.name == "bundle.mkv":
                    raise OSError("simulated atomic publication failure")
                return replace(self, target)
            with patch.object(Path, "replace", new=deny_publishing):
                with self.assertRaisesRegex(OSError, "simulated"):
                    exporter.export(
                        audio, ass, output, require_current_ass=True,
                        background_color="#424242",
                    )
            self.assertEqual(previous_mkv, output.read_bytes())
            self.assertEqual(previous_receipt, (root / RECEIPT).read_bytes())
            self.assertEqual(bundle_export_status(root)["state"], "current")

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

    def test_real_image_above_legacy_4mib_survives_final_mkv_and_reuse(self):
        """5+ MiB valid PNG is actually decoded by FFmpeg and saved for replay.

        A synthetic PNG magic header alone would test only the Base64 gate,
        not the decoded image / file-backed FFmpeg export path.
        """
        import random
        import struct
        import zlib

        width, height = 1600, 1200
        rng = random.Random(117)
        rows = b"".join(b"\x00" + rng.randbytes(width * 3) for _ in range(height))

        def chunk(name: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + name + data
                    + struct.pack(">I", zlib.crc32(name + data) & 0xffffffff))

        png = (bytes.fromhex("89504e470d0a1a0a")
               + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(rows, level=1))
               + chunk(b"IEND", b""))
        self.assertGreater(len(png), 4 * 1024 * 1024)
        self.assertLessEqual(len(png), 16 * 1024 * 1024)
        self.assertEqual(decode_bundle_background(
            base64.b64encode(png).decode("ascii")), png)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio, ass = self._audio(root), self._ass(root)
            self._certify(root)
            exporter = ArchiveBundleExporter(width=320, height=180)
            target = root / "HSR_Voice_Archive_Bundle.mkv"
            result = exporter.export(
                audio, ass, target, require_current_ass=True,
                background_image=png, background_spec={"mode": "image"},
                background_color="#162431",
            )
            self.assertEqual(result["background"], "image")
            self.assertEqual(bundle_export_status(root)["state"], "current")
            self.assertEqual(load_previous_bundle_image(root), png)
            media_streams = [row for row in exporter._probe(target)["streams"]
                             if row.get("codec_type") in {"video", "audio", "subtitle"}]
            self.assertEqual([row["codec_name"] for row in media_streams],
                             ["h264", "flac", "ass", "subrip"])
            decoded = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(target),
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            ], check=True, capture_output=True, timeout=35).stdout
            self.assertEqual(len(decoded), 320 * 180 * 3)

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
