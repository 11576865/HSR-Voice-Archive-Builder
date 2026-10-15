"""Structural contract for the rebuilt Layout/Effects authoring workbench.

This is source-level protection; the Chromium task tests remain the actual
hit-target, viewport and interaction acceptance authority.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/studio-v4.css").read_text(encoding="utf-8")
SHARED = (ROOT / "app/static/hsr-ui-convergence.css").read_text(encoding="utf-8")


class StudioV3ArchitectureTests(unittest.TestCase):
    def test_studio_has_single_stage_cue_timeline_inspector_and_render_owner(self):
        for ident in (
            "studioV3Visual", "studioV3Editors", "studioV3Inspector",
            "previewCanvasContainer", "previewQuickBtn", "previewLibassBtn", "previewVideoBtn",
            "layoutCorpusList", "layoutCueEditorPanel", "layoutCueClockOrigin",
            "previewBackgroundMode", "hsrKaraokeReadiness", "previewSaveStatus",
            "layoutAuditDrawerToggle", "layoutCorpusRail",
        ):
            self.assertEqual(HTML.count(f'id="{ident}"'), 1, ident)
        self.assertIn('class="preview-workbench studio-v3-workbench studio-v4-workbench"', HTML)
        self.assertIn('class="studio-v3-command-bar"', HTML)
        self.assertLess(HTML.index('id="studioV3Visual"'), HTML.index('id="studioV3Editors"'))
        self.assertLess(HTML.index('id="studioV3Editors"'), HTML.index('id="studioV3Inspector"'))
        self.assertLess(HTML.index('id="previewVideoBtn"'), HTML.index('id="studioV3Editors"'))
        self.assertLess(HTML.index('id="studioV3Inspector"'), HTML.index('id="prevChsFont"'))
        self.assertNotIn('id="layoutCueFinalText"', HTML)
        self.assertEqual(HTML.count('id="subFinalEditor"'), 1)

    def test_mobile_task_switcher_has_real_aria_keyboard_and_no_second_workspace(self):
        for panel, name, ctl in (
            ("cue", "studioV3CueTab", "layoutCueEditorPanel"),
            ("inspector", "studioV3InspectorTab", "studioV3Inspector"),
        ):
            self.assertEqual(HTML.count(f'id="{name}"'), 1)
            self.assertIn(f'data-studio-pane-target="{panel}" aria-controls="{ctl}"', HTML)
        self.assertNotIn('id="studioV3TimelineTab"', HTML)
        self.assertNotIn('id="layoutTimelinePanel"', HTML)
        self.assertIn("function setStudioV3Pane(pane,{focus=false}={})", HTML)
        self.assertIn("button.setAttribute('aria-selected',active?'true':'false')", HTML)
        self.assertIn("button.tabIndex=active?0:-1", HTML)
        self.assertIn("['ArrowLeft','ArrowRight']", HTML)
        self.assertIn("setStudioV3Pane('inspector')", HTML)
        self.assertIn('.studio-v3-pane-nav{display:none!important}', CSS)
        self.assertIn('studio-v4-timing', CSS)
        self.assertIn('overflow:visible!important', CSS)
        self.assertIn('@media(max-width:600px)', CSS)
        self.assertEqual(HTML.count('id="studioStyleTab"'), 1)
        self.assertEqual(HTML.count('id="studioEffectsTab"'), 1)

    def test_new_layout_isolated_and_no_old_breakpoint_ownership(self):
        self.assertEqual(HTML.count('href="/static/studio-v4.css"'), 1)
        self.assertIn("Studio V4", CSS)
        self.assertIn("grid-template-columns:minmax(0,1fr)", CSS)
        self.assertIn(".studio-v3-editors", CSS)
        self.assertIn(".studio-v3-visual>.preview-stage", CSS)
        self.assertIn("overflow:visible!important", CSS)
        self.assertNotIn("height:clamp(340px,calc(100dvh - 210px),880px)", SHARED)
        self.assertNotIn("grid-template-rows:minmax(0,44%)", SHARED)
        self.assertNotIn("position:sticky!important;top:0!important;z-index:9", CSS)
        self.assertIn('/static/assets/studio-red-clouds.webp', HTML)
        self.assertIn('use_studio_reference_background:', HTML)

    def test_core_editing_and_real_ffmpeg_callbacks_remain_single(self):
        for anchor in (
            "addEventListener('click',renderLibassPreview)",
            "addEventListener('click',renderHsrEffectVideo)",
            "addEventListener('click',saveSubtitleLayoutSettings)",
            "addEventListener('pointerdown',layoutTimelinePointerDown)",
            "layoutCueUpdateTiming(field,event.target.value)",
            "showWorkspace('review')",
            "loadWordAlignmentDiagnostics()",
        ):
            self.assertIn(anchor, HTML)


if __name__ == "__main__":
    unittest.main()
