"""FFmpeg regression: retain real ASS packet timing beyond ten hours."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.archive_bundle_exporter import ArchiveBundleExporter
from app.ass_mkv_packet_verifier import read_native_ass_signature


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Real FFmpeg / FFprobe required")
class NativeAssPacketVerificationTests(unittest.TestCase):
    def test_large_archive_over_ten_hours_and_strict_tamper_guards(self):
        # 9990 events / 4 styles match the real failing report's dimensions.
        # Subtitle-only MKV proves the exact ASS codec/timestamp boundary
        # without encoding 16 hours of dummy H.264 in the regression suite.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "archive.ass"
            mkv = root / "archive.mkv"
            styles = ("Primary", "CHS", "Card", "Archive")
            style_format = (
                "Format: Name, Fontname, Fontsize, PrimaryColour, "
                "SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
                "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
                "BorderStyle, Outline, Shadow, Alignment, MarginL, "
                "MarginR, MarginV, Encoding"
            )
            headers = (
                "[Script Info]\nTitle: Long Archive\n"
                "ScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n"
                "WrapStyle: 2\nScaledBorderAndShadow: yes\n"
                "[V4+ Styles]\n" + style_format + "\n"
                + "".join(
                    "Style: " + name
                    + ",Arial,42,&H00FFFFFF,&H00A0A0A0,&H00101010,"
                    + "&H80000000,0,0,0,0,100,100,0,0,1,2,0,8,0,0,0,1\n"
                    for name in styles
                )
                + "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, "
                "MarginR, MarginV, Effect, Text\n"
            )

            def stamp(cs: int) -> str:
                hours, remain = divmod(cs, 360000)
                minutes, remain = divmod(remain, 6000)
                seconds, fraction = divmod(remain, 100)
                return f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}"

            dialogues = []
            for number in range(9990):
                start = (number // 2) * 1200
                finish = start + 80 + (number % 3) * 20
                dialogues.append(
                    f"Dialogue: {number % 5},{stamp(start)},{stamp(finish)},"
                    f"{styles[number % 4]},,0,0,0,,"
                    + r"{\an8\pos(960,250)\fad(100,200)}"
                    + f"Cue {number} " + r"\N" + " 中文"
                )
            original = headers + "\n".join(dialogues) + "\n"
            source.write_text(original, encoding="utf-8")
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-nostdin", "-i", str(source), "-c:s", "copy", str(mkv)],
                check=True, capture_output=True,
            )
            archive_before = mkv.read_bytes()
            exporter = ArchiveBundleExporter(width=320, height=180)
            source_signature = exporter._ass_content_signature(original)
            native = read_native_ass_signature(exporter.ffprobe, mkv)
            self.assertEqual(len(native[0]), 4)
            self.assertEqual(len(native[1]), 9990)
            self.assertEqual(native, source_signature)
            exporter._verify_embedded_ass(mkv, source)

            # The native packet has 10:00:00.00 exactly, even when a build
            # of FFmpeg re-exports its textual ASS as 9:59:59.99.
            self.assertTrue(any(
                int(event[1]) >= 3600000 for event in native[1]
            ))

            cases = (
                ("text", original.replace("Cue 3521 ", "TAMPERED 3521 ", 1),
                 "字段 Text"),
                ("time", original.replace(
                    "10:00:00.00,10:00:00.80", "10:00:00.01,10:00:00.80", 1),
                 "字段 Start"),
                ("style", original.replace(
                    "Style: Card,Arial", "Style: Wrong,Arial", 1),
                 "字段 Style"),
                ("effect", original.replace(
                    r"\fad(100,200)", r"\fad(100,250)", 1),
                 "字段 Text"),
                ("count", headers + "\n".join(dialogues[:-1]) + "\n",
                 "源事件 9989/内嵌 9990"),
            )
            for name, altered, expected in cases:
                with self.subTest(name=name):
                    self.assertNotEqual(altered, original)
                    source.write_text(altered, encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, expected):
                        exporter._verify_embedded_ass(mkv, source)
                    self.assertEqual(mkv.read_bytes(), archive_before)
            source.write_text(original, encoding="utf-8")
            exporter._verify_embedded_ass(mkv, source)


if __name__ == "__main__":
    unittest.main()
