"""Regression for user-reported duplicate glyphs, imported-font fallback and 60/40 layout."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from subtitle_layout.layout_solver import solve_subtitle_layout
from subtitle_layout.config import SubtitleRenderConfig
from app.ass_preview import _project_font_config, _assert_project_font_selected, _find_fc_match

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
                selected_log=f"fontselect: (ProjectChinese, 400, 0) -> {path.resolve()}, 0, ProjectChinese"
                result=type("MatchResult",(),{"stdout":str(path),"returncode":0})()
                with patch("app.ass_preview.subprocess.run",return_value=result):
                    _assert_project_font_selected(selected_log,resolved,fonts,
                                                  english="",chinese="汉字")
                false_success="fontselect: (ProjectChinese, 400, 0) -> /host/fallback.ttf, 0, Fallback"
                with patch("app.ass_preview.subprocess.run",return_value=result):
                    with self.assertRaisesRegex(RuntimeError,"libass.*不匹配的字体面"):
                        _assert_project_font_selected(false_success,resolved,fonts,
                                                      english="",chinese="汉字")
                # Some libass builds report PostScript face names, not paths.
                # An exact matching face is valid only alongside fc-match's
                # independent verified uploaded-file identity.
                face_only="fontselect: (ProjectChinese, 400, 0) -> ProjectChinese-Regular, 0"
                with patch("app.ass_preview.subprocess.run",return_value=result):
                    _assert_project_font_selected(face_only,resolved,fonts,
                                                  english="",chinese="汉字")
                wrong_face="fontselect: (ProjectChinese, 400, 0) -> NotoSans, 0"
                with patch("app.ass_preview.subprocess.run",return_value=result):
                    with self.assertRaisesRegex(RuntimeError,"不匹配的字体面"):
                        _assert_project_font_selected(wrong_face,resolved,fonts,
                                                      english="",chinese="汉字")
                # A valid fc-match is insufficient if libass reports missing
                # glyphs and silently chooses a different fallback face.
                missing_glyph=f"{selected_log}\\n[Parsed_subtitles_0] Glyph 0x6C49 not found, selecting one more font"
                with patch("app.ass_preview.subprocess.run",return_value=result):
                    with self.assertRaisesRegex(RuntimeError,"缺少当前字幕中的字形 U\\+6C49"):
                        _assert_project_font_selected(missing_glyph,resolved,fonts,
                                                      english="",chinese="汉字")
                bad=type("MatchResult",(),{"stdout":"/host/fallback.ttf","returncode":0})()
                with patch("app.ass_preview.subprocess.run",return_value=bad):
                    with self.assertRaisesRegex(RuntimeError,"被回退"):
                        _assert_project_font_selected(selected_log,resolved,fonts,
                                                      english="",chinese="汉字")

    def test_fc_match_absolute_termux_prefix_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"bin"/"fc-match"
            path.parent.mkdir()
            path.write_text("#!/bin/sh\n",encoding="utf-8")
            path.chmod(0o755)
            with patch("app.ass_preview.shutil.which",return_value=None), patch.dict(
                "app.ass_preview.os.environ",{"PREFIX":str(Path(td))}
            ):
                self.assertEqual(_find_fc_match(),str(path))
                path.chmod(0o644)
                self.assertIsNone(_find_fc_match())

    def test_html_has_single_geometry_glyph_layer_and_region_control(self):
        html=(ROOT/"app/static/index.html").read_text("utf-8")
        css=(ROOT/"app/static/studio-v4.css").read_text("utf-8")
        self.assertEqual(html.count('id="prevPrimaryShare"'),1)
        self.assertIn("primary_region_share:primaryShare/100",html)
        self.assertIn("primary_region_share: Number(document.getElementById('prevPrimaryShare')",html)
        self.assertIn("overlay.style.display='none'",html)
        self.assertIn("const overlayVisible=geometryExplicitlyRequested",html)
        self.assertIn("document.getElementById('previewSvg')?.classList.toggle('hidden',!overlayVisible)",html)
        self.assertIn("queueLiveRealPreview()",html)
        self.assertIn("await renderLibassPreview()",html)
        self.assertIn("studio-region-share + #prevPrimaryShare",css)
        self.assertEqual(html.count('id="studioRegionPercent"'),1)
        self.assertIn('data-share-percent="60"',html)
        self.assertIn('data-share-percent="50"',html)
        self.assertIn('data-share-percent="40"',html)
        self.assertIn('function commitStudioShare(value)',html)
        self.assertIn("const svg=document.getElementById('previewSvg')",html)
        self.assertIn("canvas.getContext?.('2d')?.clearRect",html)
        self.assertIn("img.removeAttribute?.('src')",html)
        self.assertIn("fontChoiceSaved=false",html)
        self.assertIn("if(!fontChoiceSaved&&field",html)
        self.assertNotIn('id="projectFontRefreshBtn"',html)
        self.assertNotIn("function applySubtitlePreset",html)
        self.assertIn("id=\"studioV4Timing\"",html)
        self.assertNotIn('#subtitleLayoutPreviewCard[data-studio-mode="style"] #layoutTimelinePanel,',html)
        self.assertNotIn("renderCanvasSubtitleOverlay(data);",html)
        self.assertIn("#assCanvasPreview{display:none!important}",css)
        self.assertIn("studioV4Timing[open] #layoutTimelinePanel",css)
        self.assertIn('id="projectFontImporter"',html)
        self.assertIn("generateButton.hidden=!provider.available",html)

    def test_actual_ass_is_the_authoritative_layout_editing_surface(self):
        html=(ROOT/"app/static/index.html").read_text("utf-8")
        self.assertIn("let lastRenderedAssIdentity = null;",html)
        self.assertIn("lastRenderedAssIdentity=identity;",html)
        self.assertIn("lastRenderedAssIdentity!==currentLibassPreviewIdentity()",html)
        self.assertIn("queueLiveRealPreview();",html)
        self.assertIn("liveRealPreviewArmed=true;",html)
        self.assertIn("几何估算通过 · 非真实验收",html)
        self.assertIn("不代表全项目通过",html)
        self.assertIn("叠加诊断线",html)
        self.assertNotIn("geometryStyledText(",html)
        self.assertIn("辅助边界图（无文字渲染）",html)

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
