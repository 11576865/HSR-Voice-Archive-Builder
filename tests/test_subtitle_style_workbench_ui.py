from __future__ import annotations

import unittest
from pathlib import Path


class SubtitleStyleWorkbenchUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

    def test_style_workbench_uses_adaptive_split_pane_shell(self) -> None:
        html = self.html
        self.assertIn("字幕样式工作台", html)
        self.assertNotIn(">字幕排版工作台</h2>", html)
        self.assertIn("SUBTITLE STYLE WORKBENCH", html)
        self.assertIn("STYLE INSPECTOR", html)
        self.assertIn("Subtitle Style Workbench redesign v14", html)
        self.assertIn(
            "grid-template-columns:minmax(0,1fr) clamp(390px,26vw,460px)!important",
            html,
        )
        self.assertIn("aspect-ratio:16 / 9!important", html)
        self.assertIn("max-height:calc(100dvh - var(--topbar-height) - 58px)!important", html)

    def test_redesign_resets_legacy_grid_placement_and_padding(self) -> None:
        html = self.html
        self.assertNotIn(
            "padding-right:var(--layout-inspector-width)!important",
            html,
        )
        self.assertNotIn(
            "width:var(--layout-inspector-width)!important",
            html,
        )
        self.assertGreaterEqual(html.count("grid-area:auto!important"), 2)
        self.assertIn(
            'body[data-workspace="layout"] .preview-workbench{\n'
            '    width:100%!important;\n'
            '    padding-right:0!important;',
            html,
        )

    def test_primary_actions_stay_above_long_parameter_stack(self) -> None:
        html = self.html
        action_pos = html.index('class="preview-actions"')
        controls_pos = html.index('class="preview-controls"')
        self.assertLess(action_pos, controls_pos)
        self.assertIn('id="previewGeometryBtn"', html)
        self.assertIn('id="previewLibassBtn"', html)
        self.assertIn('id="saveSubtitleLayoutBtn"', html)
        self.assertIn('id="generateSubtitleAssBtn"', html)
        self.assertIn("#generateSubtitleAssBtn{", html)
        self.assertIn("grid-column:1 / -1", html)

    def test_basic_and_advanced_controls_are_grouped_without_id_changes(self) -> None:
        html = self.html
        for title in (
            "文字与字体",
            "位置与安全区",
            "预设",
            "外观与可读性",
            "动效",
            "逐词同步",
            "档案增强",
            "画布与诊断",
        ):
            self.assertIn(title, html)

        for control_id in (
            "prevChsFont",
            "prevPriFont",
            "prevChsSize",
            "prevPriSize",
            "prevCentralGap",
            "prevMarginH",
            "prevMarginV",
            "prevOutlineWidth",
            "prevShadowDepth",
            "prevBlurRadius",
            "prevFadeIn",
            "prevFadeOut",
            "prevEnableKaraoke",
            "prevEnableArchiveHud",
            "previewBackgroundMode",
        ):
            self.assertIn(f'id="{control_id}"', html)

        self.assertIn('class="layout-inspector-section layout-section-advanced"', html)

    def test_tablet_and_mobile_breakpoints_keep_single_column_flow(self) -> None:
        html = self.html
        self.assertIn("@media (min-width:760px) and (max-width:1200px)", html)
        self.assertIn("@media (max-width:759px)", html)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))!important", html)
        self.assertIn("grid-template-columns:1fr!important", html)


if __name__ == "__main__":
    unittest.main()
