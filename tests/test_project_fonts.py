"""Project font ingestion and real FFmpeg/libass font-folder smoke test."""
from __future__ import annotations

import base64
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from app.ass_preview import preview_config_from_payload, render_ass_preview_png
from app.project_fonts import (
    import_project_font,
    list_project_fonts,
    project_font_for_family,
    font_asset_directory,
)


def fake_name_table_font(family: str = "Example Family") -> bytes:
    label = family.encode("utf-16-be")
    names = struct.pack(">HHH", 0, 1, 18)
    names += struct.pack(">HHHHHH", 3, 1, 1033, 1, len(label), 0)
    names += label
    return (b"\x00\x01\x00\x00" + struct.pack(">HHHH", 1, 0, 0, 0) +
            struct.pack(">4sIII", b"name", 0, 28, len(names)) + names)


class ProjectFontTests(unittest.TestCase):
    def test_import_family_identity_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            blob = base64.b64encode(fake_name_table_font()).decode("ascii")
            added = import_project_font(root, "../../evil.ttf", blob)
            self.assertEqual(added["family"], "Example Family")
            self.assertTrue(added["filename"].endswith(".ttf"))
            self.assertNotIn("evil", added["filename"])
            self.assertEqual(len(list_project_fonts(root)), 1)
            same = import_project_font(root, "copy.ttf", blob)
            self.assertEqual(same["filename"], added["filename"])
            self.assertEqual(len(list_project_fonts(root)), 1)
            self.assertEqual(
                project_font_for_family(root, "example family"),
                font_asset_directory(root) / added["filename"],
            )

    def test_reject_non_fonts_and_invalid_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = base64.b64encode(b"this is not a font").decode("ascii")
            for filename in ("malicious.svg", "m.exe"):
                with self.assertRaises(ValueError):
                    import_project_font(root, filename, data)
            with self.assertRaises(ValueError):
                import_project_font(root, "wrong.ttf", data)
            with self.assertRaises(ValueError):
                import_project_font(root, "wrong.ttf", "not base64 ??")
            with self.assertRaises(ValueError):
                import_project_font(root, "huge.ttf", "A" * (17 * 1024 * 1024))
            self.assertFalse(font_asset_directory(root).exists())

    @unittest.skipUnless(shutil.which("ffmpeg"), "Real FFmpeg unavailable")
    def test_real_libass_can_read_imported_ttf_from_project_folder(self):
        try:
            from subtitle_layout.fonts import resolve_font_path
            candidate = resolve_font_path("DejaVu Sans")
        except (ValueError, FileNotFoundError):
            candidate = None
        if not candidate or not Path(candidate).is_file():
            self.skipTest("No installed DejaVu Sans font suitable for FFmpeg fixture")
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            font_bytes = Path(candidate).read_bytes()
            font = import_project_font(
                output, Path(candidate).name,
                base64.b64encode(font_bytes).decode("ascii"),
            )
            cfg = preview_config_from_payload({
                "chs_font": font["family"],
                "primary_font": font["family"],
                "enable_kinetic": False,
            })
            actual = render_ass_preview_png(
                english_text="Project font",
                chinese_text="Subtitle preview",
                source_language="en",
                target_language="en",
                config=cfg,
                duration_seconds=1.5,
                timestamp=0.5,
                font_dir=font_asset_directory(output),
            )
            self.assertTrue(actual.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertGreater(len(actual), 1500)


if __name__ == "__main__":
    unittest.main()
