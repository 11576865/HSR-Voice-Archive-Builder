"""Real FFmpeg+ffprobe acceptance for a selectable ASS+FLAC+font MKV artifact."""
from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.archive_bundle_exporter import ArchiveBundleExporter, decode_bundle_background


class BundleInputValidationTests(unittest.TestCase):
    def test_background_is_bounded_and_sniffed(self):
        with self.assertRaisesRegex(ValueError, "PNG, JPEG or WebP"):
            decode_bundle_background(base64.b64encode(b"not an image").decode("ascii"))
        with self.assertRaises(ValueError):
            decode_bundle_background("garbage!")
        with self.assertRaises(ValueError):
            decode_bundle_background("A" * (6 * 1024 * 1024))
        self.assertIsNone(decode_bundle_background(None))


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
