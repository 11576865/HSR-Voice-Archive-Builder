"""Acceptance contracts for explicit inner/outer ASS and shared red-cloud background."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.archive_bundle_backgrounds import load_default_red_cloud_background
from app.archive_bundle_lifecycle import (
    load_previous_bundle_image, previous_bundle_settings,
    record_bundle_publication,
)
from app.ass_preview import load_studio_reference_background
from app.project import ProjectConfig
from app.subtitles import export_independent_ass
from subtitle_layout.config import SubtitleRenderConfig, normalize_subtitle_color
from subtitle_layout.ass_writer import render_ass
from types import SimpleNamespace


class RedCloudAndStandaloneAssTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.config = ProjectConfig(
            schema_version=1, name="HSR-fixture", root=str(self.root),
            index_csv="index.csv", wav_source="wavs", output_dir="output",
        )

    def test_new_mkv_background_is_byte_identical_to_studio_red_cloud(self):
        settings = previous_bundle_settings(self.output)
        self.assertFalse(settings["known"])
        self.assertEqual(settings["background_spec"]["mode"], "image")
        self.assertTrue(settings["image_saved"])
        self.assertEqual(load_previous_bundle_image(self.output),
                         load_studio_reference_background())
        self.assertEqual(load_previous_bundle_image(self.output),
                         load_default_red_cloud_background())

    def test_user_saved_color_is_not_replaced_by_default_red_cloud(self):
        (self.output / "HSR_Voice_Archive_Bundle.mkv").write_bytes(b"existing")
        for name, content in (
            ("manifest.json", '{"entries":[]}'),
            ("HSR_Voice_Archive.ass", "[Events]"),
            ("HSR_Voice_Archive.srt", "1"),
            ("continuous.flac", "FLAC"),
        ):
            (self.output / name).write_text(content)
        from app.subtitle_export_status import record_subtitle_export
        record_subtitle_export(self.output, state="current", ass_required=True)
        record_bundle_publication(
            self.output, background_color="#112233",
            background_image=None, image_extension=None, include_fonts=False,
            background_spec={"mode": "color"},
        )
        self.assertEqual(previous_bundle_settings(self.output)["background_spec"]["mode"], "color")
        with self.assertRaisesRegex(ValueError, "没有可复用"):
            load_previous_bundle_image(self.output)

    def test_external_ass_never_mutates_legacy_ass_mkv_receipts_or_srt(self):
        (self.output / "manifest.json").write_text(json.dumps({"entries": [
            {"index": 1, "source_text": "Hello", "target_text": "你好",
             "start_seconds": 0.5, "audio_end_seconds": 1.4},
        ]}), encoding="utf-8")
        protected = {}
        for name in ("HSR_Voice_Archive.ass", "HSR_Voice_Archive.srt",
                     "HSR_Voice_Archive_Bundle.mkv", "subtitle_export_receipt.json"):
            content = "prior-" + name
            (self.output / name).write_text(content, encoding="utf-8")
            protected[name] = content
        def fake_writer(entries, path, **kwargs):
            path.write_text("new independent ASS\n", encoding="utf-8")
            callback = kwargs.get("progress_callback")
            if callback: callback(1, 1)
        with patch("app.subtitles.subtitle_render_config",
                   return_value=SubtitleRenderConfig.plain_text_preset()), \
             patch("app.subtitles.write_ass", side_effect=fake_writer):
            result1 = export_independent_ass(self.config, self.output)
            result2 = export_independent_ass(self.config, self.output)
        self.assertNotEqual(result1["ass_file"], result2["ass_file"])
        self.assertEqual(Path(result1["ass_file"]).read_text(), "new independent ASS\n")
        self.assertEqual(Path(result2["ass_file"]).read_text(), "new independent ASS\n")
        for name, content in protected.items():
            self.assertEqual((self.output / name).read_text(encoding="utf-8"), content)
        self.assertTrue(result1["mkv_untouched"])

    def test_primary_and_chinese_colors_and_disabled_legacy_animation(self):
        self.assertEqual(normalize_subtitle_color("#c9e3ff"), "#C9E3FF")
        with self.assertRaises(ValueError):
            normalize_subtitle_color("red};\\move(0,0,10,10)")
        ass = render_ass([SimpleNamespace(
            english="Hello", chinese="你好",
            start_seconds=1.0, audio_end_seconds=2.0, display_end_seconds=2.0,
        )], config=SubtitleRenderConfig(
            primary_color="#ABCDEF", chs_color="#123456",
            enable_soft_entry=True, enable_kinetic=True,
            fade_in_ms=600, fade_out_ms=600,
        ))
        self.assertIn("Style: Primary,", ass)
        self.assertIn("&H00EFCDAB", ass)
        self.assertIn("&H00563412", ass)
        self.assertNotIn(r"\move(", ass)
        self.assertIn(r"\fad(", ass)
        self.assertNotIn(r"\fscx", ass)

    def test_six_hundred_ms_fade_is_serialized_in_both_language_events(self):
        cfg = SubtitleRenderConfig(
            primary_color="#C9E3FF", chs_color="#FFFFFF",
            fade_in_ms=600, fade_out_ms=600, enable_kinetic=True,
        )
        entries = [
            SimpleNamespace(english="Hello", chinese="你好",
                            start_seconds=1.0, audio_end_seconds=2.0,
                            display_end_seconds=2.0),
            SimpleNamespace(english="World", chinese="世界",
                            start_seconds=3.5, audio_end_seconds=4.5,
                            display_end_seconds=4.5),
        ]
        ass = render_ass(entries, config=cfg)
        lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        first = [line for line in lines if "0:00:00.40,0:00:02.60" in line]
        self.assertEqual(len([line for line in first if ",Primary," in line]), 1)
        self.assertEqual(len([line for line in first if ",CHS," in line]), 1)
        self.assertTrue(all(r"\fad(600,600)" in line for line in first))
        # Neither voice moment is consumed by transitions, and silence remains.
        self.assertIn("0:00:02.90,0:00:04.50", ass)

    def test_browser_and_servers_expose_two_distinct_user_commands(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "app/static/index.html").read_text(encoding="utf-8")
        self.assertIn("生成 ASS（内）", html)
        self.assertIn("生成 ASS（外）", html)
        self.assertIn("'/api/output/ass/external'", html)
        self.assertIn("startAssExport('internal')", html)
        self.assertIn("startAssExport('external')", html)
        self.assertIn('id="prevPrimaryColor"', html)
        self.assertIn('id="prevChsColor"', html)
        self.assertIn('fieldset hidden disabled style="display:none"', html)
        for path in ("app/server.py", "app/lite_server.py"):
            source = (root / path).read_text(encoding="utf-8")
            self.assertIn('"/api/output/ass/external"', source)
            self.assertIn("queue_external_ass_export(config, output)", source)
            self.assertIn("normalize_subtitle_color", source)

    def test_style_autosave_does_not_invoke_export_or_video_renderer(self):
        html = (Path(__file__).resolve().parents[1] /
                "app/static/index.html").read_text(encoding="utf-8")
        start = html.index("async function saveSubtitleLayoutSettings(")
        end = html.index("// Shared immutable", start)
        save = html[start:end]
        self.assertIn("'/api/subtitle-layout/settings'", save)
        for forbidden in ("startAssExport(", "renderHsrEffectVideo(",
                          "renderLibassPreview(", "/api/output/ass"):
            self.assertNotIn(forbidden, save)



if __name__ == "__main__":
    unittest.main()
