from __future__ import annotations
import unittest
from types import SimpleNamespace
from pathlib import Path
from subtitle_layout import render_ass, solve_subtitle_layout
from subtitle_layout.config import SubtitleRenderConfig
from subtitle_layout.preview import preview_subtitle_layout

class RatioSafetyContractTests(unittest.TestCase):
    def test_default_and_configured_split(self):
        for ratio in (0.20, 0.60, 0.80):
            result = preview_subtitle_layout('Hello', '你好', primary_region_ratio=ratio)
            gap = result['central_gap']
            self.assertEqual(gap['primary_region_ratio'], ratio)
            self.assertEqual(gap['y_top'], round(54 + (972-20) * ratio))
            self.assertEqual(gap['y_bottom'], gap['y_top']+20)
            self.assertEqual(result['safe_area']['y_min'], 54)
            self.assertEqual(result['layout']['evidence'], 'layout_metrics_only_not_libass_ink')
    def test_ratio_matches_real_ass_positions_without_duplicate_margins(self):
        ratio=0.7
        solved=solve_subtitle_layout('Hello','你好',primary_region_ratio=ratio)
        out=render_ass([SimpleNamespace(english='Hello',chinese='你好',start_seconds=1,display_end_seconds=3)],config=SubtitleRenderConfig(primary_region_ratio=ratio),enable_kinetic=False)
        self.assertIn('\\pos(960,'+str(solved.primary_lines[0].y)+')', out)
        self.assertIn('\\pos(960,'+str(solved.chs_lines[0].y)+')', out)
        self.assertEqual([l.endswith(',8,0,0,0,1') for l in out.splitlines() if l.startswith(('Style: CHS,','Style: Primary,'))],[True,True])
    def test_ratio_clamps_and_ui_is_connected(self):
        self.assertEqual(preview_subtitle_layout('Hi','你好',primary_region_ratio=0)['central_gap']['primary_region_ratio'],0.2)
        self.assertEqual(preview_subtitle_layout('Hi','你好',primary_region_ratio=1)['central_gap']['primary_region_ratio'],0.8)
        html=(Path(__file__).resolve().parents[1]/'app'/'static'/'index.html').read_text('utf-8')
        self.assertIn('id="prevPrimaryRatio"',html)
        self.assertIn('primary_region_ratio: primaryRatio',html)
        self.assertIn('subtitle_primary_region_ratio??0.60',html)
        self.assertIn('id="prevShowBBox">',html)
        self.assertIn("addEventListener('change',()=>renderLibassPreview())",html)

if __name__=='__main__': unittest.main()
