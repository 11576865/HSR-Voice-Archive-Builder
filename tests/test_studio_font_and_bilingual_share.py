"""Regression for user-reported duplicate glyphs, imported-font fallback and 60/40 layout."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from subtitle_layout.layout_solver import solve_subtitle_layout
from subtitle_layout.config import SubtitleRenderConfig
from app.ass_preview import _project_font_config, _assert_project_font_selected

ROOT=Path(__file__).resolve().parents[1]


class StudioFontAndRegionTests(unittest.TestCase):
    def test_region_share_changes_actual_solver_coordinates(self):
        kwargs=dict(english_text="A short English subtitle",
                    chinese_text="这是一句中文",source_language="en",target_language="zh-CN")
        a=solve_subtitle_layout(**kwargs,primary_region_share=0.30)
        b=solve_subtitle_layout(**kwargs,primary_region_share=0.70)
        self.assertFalse(a.failed)
        self.assertFalse(b.failed)
        self.assertNotEqual(a.primary_lines[0].y,b.primary_lines[0].y)
        self.assertNotEqual(a.chs_lines[0].y,b.chs_lines[0].y)
        self.assertLess(a.primary_lines[0].y,b.primary_lines[0].y)
        self.assertLess(a.chs_lines[0].y,b.chs_lines[0].y)

    def test_project_font_configuration_overrides_host_resolution(self):
        cfg=SubtitleRenderConfig(chs_font="ProjectChinese",primary_font="HostLatin")
        with tempfile.TemporaryDirectory() as td:
            fonts=Path(td)/"subtitle_fonts"
            fonts.mkdir()
            path=fonts/"f0a00d00.ttf"
            path.write_bytes(b"fixture")
            with patch("app.ass_preview.project_font_for_family",
                       side_effect=lambda output,family: path if family=="ProjectChinese" else None):
                resolved=_project_font_config(cfg,fonts)
                self.assertEqual(resolved.chs_font_path,str(path))
                self.assertEqual(resolved.primary_font_path,None)
                selected_log="fontselect: (ProjectChinese, 400, 0) -> "+str(path)+", 0, ProjectChinese"
                _assert_project_font_selected(selected_log,resolved,fonts,
                                              english="",chinese="汉字")
                with self.assertRaisesRegex(RuntimeError,"导入字体未用于真实 ASS 渲染"):
                    _assert_project_font_selected("fontselect: (ProjectChinese, 400, 0) -> /host/fallback.ttf",
                                                  resolved,fonts,english="",chinese="汉字")

    def test_html_has_single_geometry_glyph_layer_and_region_control(self):
        html=(ROOT/"app/static/index.html").read_text("utf-8")
        css=(ROOT/"app/static/studio-v4.css").read_text("utf-8")
        self.assertEqual(html.count('id="prevPrimaryShare"'),1)
        self.assertIn("primary_region_share:primaryShare/100",html)
        self.assertIn("primary_region_share: Number(document.getElementById('prevPrimaryShare')",html)
        self.assertIn("overlay?.getContext('2d')?.clearRect",html)
        self.assertNotIn("renderCanvasSubtitleOverlay(data);",html)
        self.assertIn("#assCanvasPreview{display:none!important}",css)
        self.assertIn("studioV4Timing[open] #layoutTimelinePanel",css)
        self.assertIn('id="projectFontRefreshBtn" class="secondary" type="button" hidden',html)
        self.assertIn("generateButton.hidden=!provider.available",html)

    def test_persistent_share_wired_both_server_flavors_and_final_export(self):
        project=(ROOT/"app/project.py").read_text("utf-8")
        backend=(ROOT/"app/server.py").read_text("utf-8")
        lite=(ROOT/"app/lite_server.py").read_text("utf-8")
        export=(ROOT/"app/subtitles.py").read_text("utf-8")
        preview=(ROOT/"subtitle_layout/preview.py").read_text("utf-8")
        writer=(ROOT/"subtitle_layout/ass_writer.py").read_text("utf-8")
        self.assertIn("subtitle_primary_region_share: float = 0.60",project)
        self.assertIn("subtitle_primary_region_share=max(",backend)
        self.assertIn("subtitle_primary_region_share=max(",lite)
        self.assertIn("primary_region_share=max(0.20",export)
        self.assertIn("primary_region_share=primary_region_share",preview)
        self.assertIn("primary_region_share=float(cfg.primary_region_share)",writer)


if __name__=="__main__":
    unittest.main()
