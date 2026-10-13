"""Real >4 MiB PNG runs through FFmpeg preview, MKV, project retention and replay."""
from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

from app.archive_bundle_backgrounds import normalize_bundle_background, render_background_preview
from app.archive_bundle_exporter import ArchiveBundleExporter, decode_bundle_background
from app.archive_bundle_lifecycle import bundle_export_status, load_previous_bundle_image
from app.subtitle_export_status import record_subtitle_export


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def _large_real_png() -> bytes:
    """Incompressible 1500x1300 RGB PNG (~5.6 MiB), no Pillow or extra deps."""
    width, height = 1500, 1300
    scanline = b"".join(b"\x00" + os.urandom(width * 3) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(scanline, level=1))
            + _png_chunk(b"IEND", b""))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Actual FFmpeg and FFprobe required")
class LargeBackgroundImageAcceptanceTests(unittest.TestCase):
    def test_real_image_over_old_four_mib_is_previewed_exported_and_reused(self):
        raw = _large_real_png()
        self.assertGreater(len(raw), 4 * 1024 * 1024)
        self.assertLess(len(raw), 16 * 1024 * 1024)
        spec = normalize_bundle_background({"mode": "image", "resolution": "720p"})
        with tempfile.TemporaryDirectory(prefix="hsr-large-bg-") as td:
            output = Path(td)
            (output / "manifest.json").write_text('{"entries":[]}', encoding="utf-8")
            (output / "HSR_Voice_Archive.srt").write_text("1\n", encoding="utf-8")
            audio = output / "continuous.flac"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=480:duration=1.2",
                "-c:a", "flac", str(audio),
            ], check=True, capture_output=True, timeout=30)
            subtitle = output / "HSR_Voice_Archive.ass"
            subtitle.write_text(
                "[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 180\n"
                "[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                "Style: Default,Arial,20,&H00FFFFFF,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n"
                "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Archive\n",
                encoding="utf-8",
            )
            record_subtitle_export(output, state="current", ass_required=True)
            self.assertEqual(decode_bundle_background(__import__("base64").b64encode(raw).decode()), raw)
            first_png = render_background_preview(spec, color="#000000", image=raw)
            self.assertTrue(first_png.startswith(b"\x89PNG\r\n\x1a\n"))
            target = output / "HSR_Voice_Archive_Bundle.mkv"
            exporter = ArchiveBundleExporter(width=320, height=180)
            published = exporter.export(
                audio, subtitle, target, require_current_ass=True,
                background_image=raw, background_spec=spec, include_fonts=False,
            )
            self.assertEqual(published["background"], "image")
            self.assertEqual(bundle_export_status(output)["state"], "current")
            reused = load_previous_bundle_image(output)
            self.assertEqual(hashlib.sha256(reused).digest(), hashlib.sha256(raw).digest())
            self.assertEqual(len(reused), len(raw))
            second_png = render_background_preview(spec, color="#000000", image=reused)
            self.assertEqual(hashlib.sha256(first_png).digest(), hashlib.sha256(second_png).digest())
            streams = exporter._probe(target)["streams"]
            stream_codecs = {s["codec_type"]: s["codec_name"] for s in streams}
            for kind, codec in {"video": "h264", "audio": "flac", "subtitle": "ass"}.items():
                self.assertEqual(stream_codecs[kind], codec)


if __name__ == "__main__":
    unittest.main()
