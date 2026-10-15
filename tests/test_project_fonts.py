"""Project font ingestion and real FFmpeg/libass font-folder smoke test."""
from __future__ import annotations

import base64
import shutil
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from app.ass_preview import preview_config_from_payload, render_ass_preview_png, _assert_project_font_selected
from app.project_fonts import (
    import_project_font,
    list_project_fonts,
    project_font_for_family,
    font_asset_directory,
    project_font_face_names,
)


def fake_name_table_font(family: str = "Example Family",
                         postscript: str | None = None) -> bytes:
    fields = [(1, family)]
    if postscript is not None:
        fields.append((6, postscript))
    strings = b""
    records = b""
    for name_id, name in fields:
        label = name.encode("utf-16-be")
        records += struct.pack(">HHHHHH", 3, 1, 1033, name_id,
                               len(label), len(strings))
        strings += label
    names = struct.pack(">HHH", 0, len(fields), 6 + 12 * len(fields))
    names += records + strings
    return (b"\x00\x01\x00\x00" + struct.pack(">HHHH", 1, 0, 0, 0) +
            struct.pack(">4sIII", b"name", 0, 28, len(names)) + names)


class ProjectFontTests(unittest.TestCase):
    def test_nonstandard_postscript_face_is_verified_from_imported_sfnt(self):
        # HYRunYuan-55W may be the PostScript face for family HYRunYuan.
        # Reject unrelated faces instead of accepting an arbitrary prefix.
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            blob = fake_name_table_font("HYRunYuan", "HYRunYuan-55W")
            added = import_project_font(
                output, "HYRunYuan-55W.ttf",
                base64.b64encode(blob).decode("ascii"))
            font_file = font_asset_directory(output) / added["filename"]
            self.assertIn("HYRunYuan-55W", project_font_face_names(font_file))
            cfg = preview_config_from_payload({
                "chs_font": "HYRunYuan", "primary_font": "Noto Sans"
            })
            matched = SimpleNamespace(stdout=str(font_file.resolve()) + "\n",
                                      returncode=0)
            with patch("app.ass_preview.shutil.which",
                       return_value="/usr/bin/fc-match"), \
                 patch("app.ass_preview.subprocess.run", return_value=matched):
                _assert_project_font_selected(
                    "fontselect: (HYRunYuan, 400, 0) -> HYRunYuan-55W, 0",
                    cfg, font_asset_directory(output),
                    english="", chinese="测试")
                with self.assertRaisesRegex(RuntimeError, "不匹配的字体面"):
                    _assert_project_font_selected(
                        "fontselect: (HYRunYuan, 400, 0) -> HYRunYuan-66W, 0",
                        cfg, font_asset_directory(output),
                        english="", chinese="测试")

    def test_termux_launchers_install_native_pillow_for_real_font_metrics(self):
        root=Path(__file__).resolve().parents[1]
        for name in ("run_termux.sh","run_termux_lan.sh"):
            source=(root/name).read_text("utf-8")
            self.assertIn("pkg install -y python-pillow",source)
            self.assertIn("from PIL import ImageFont",source)
            self.assertIn("Missing imported-font measurement support",source)

    def test_termux_launchers_install_fontconfig_utils_and_verify_executable(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("run_termux.sh", "run_termux_lan.sh"):
            with self.subTest(name=name):
                script = (root / name).read_text("utf-8")
                self.assertIn(
                    "command -v fc-match >/dev/null 2>&1 || pkg install -y fontconfig-utils",
                    script)
                self.assertIn("if ! command -v fc-match >/dev/null 2>&1; then", script)
                self.assertIn("exit 1", script)
                self.assertNotIn("pkg install -y fontconfig\n", script)

    def test_termux_fontconfig_runtime_guidance_names_providing_package(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("app/ass_preview.py", "app/preflight.py", "docs/termux.md"):
            with self.subTest(name=name):
                text = (root / name).read_text("utf-8")
                self.assertIn("fontconfig-utils", text)

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

    def test_reimport_same_family_replaces_old_asset_and_fontconfig_choice(self):
        # Different bytes, identical SFNT family: keeping both gives fontsdir
        # two eligible faces, and libass may select the previous SHA filename.
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            original = fake_name_table_font("Replacement Family")
            revised = original + b"\\x00"  # same internal family, different SHA
            first = import_project_font(
                output, "first.ttf", base64.b64encode(original).decode("ascii"))
            second = import_project_font(
                output, "second.ttf", base64.b64encode(revised).decode("ascii"))
            self.assertNotEqual(first["filename"], second["filename"])
            self.assertEqual(second["replaced_files"], [first["filename"]])
            self.assertFalse((font_asset_directory(output) / first["filename"]).exists())
            self.assertEqual(len(list_project_fonts(output)), 1)
            selected = project_font_for_family(output, "replacement family")
            self.assertEqual(selected, font_asset_directory(output) / second["filename"])
            self.assertEqual(selected.read_bytes(), revised)
            repeated = import_project_font(
                output, "again.ttf", base64.b64encode(revised).decode("ascii"))
            self.assertEqual(repeated["replaced_files"], [])
            self.assertEqual(len(list_project_fonts(output)), 1)

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
    def test_real_libass_uses_reimported_same_family_font(self):
        try:
            from subtitle_layout.fonts import resolve_font_path
            path = resolve_font_path("DejaVu Sans")
        except (ValueError, FileNotFoundError):
            path = None
        if not path or not Path(path).is_file():
            self.skipTest("Installed DejaVu Sans required for real FFmpeg regression")
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            original = Path(path).read_bytes()
            first = import_project_font(
                output, "first.ttf", base64.b64encode(original).decode("ascii"))
            # Appending a byte leaves the SFNT table offsets, checksums and glyphs
            # unchanged but changes the content-addressed project asset identity.
            replacement = original + bytes([0])
            second = import_project_font(
                output, "replacement.ttf",
                base64.b64encode(replacement).decode("ascii"))
            self.assertEqual(first["family"], second["family"])
            self.assertEqual(second["replaced_files"], [first["filename"]])
            cfg = preview_config_from_payload({
                "chs_font": second["family"], "primary_font": second["family"],
                "enable_kinetic": False,
            })
            frame = render_ass_preview_png(
                english_text="Second imported font",
                chinese_text="Updated font preview",
                source_language="en", target_language="en",
                config=cfg, duration_seconds=1.5, timestamp=0.5,
                font_dir=font_asset_directory(output))
            self.assertTrue(frame.startswith(bytes([137, 80, 78, 71, 13, 10, 26, 10])))
            self.assertGreater(len(frame), 1500)

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
