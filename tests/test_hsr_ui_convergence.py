"""Regression contract for HSR subtitle UI task separation and reduced duplicate controls."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "app/static/index.html"
CSS = ROOT / "app/static/hsr-ui-convergence.css"


class HsrUiConvergence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")
        cls.css = CSS.read_text(encoding="utf-8")

    def test_task_navigation_outside_parameter_inspector(self):
        doc = self.html
        self.assertEqual(doc.count('id="studioStyleTab"'), 1)
        self.assertEqual(doc.count('id="studioEffectsTab"'), 1)
        self.assertLess(doc.index('id="studioStyleTab"'), doc.index('class="preview-workbench"'))
        self.assertLess(doc.index('id="studioEffectsTab"'), doc.index('class="preview-workbench"'))
        self.assertIn('id="hsrInspectorTitle"', doc)
        self.assertIn("?'特效参数':'排版参数'", doc)

    def test_effects_is_a_real_primary_navigation_destination_without_cloned_state(self):
        doc = self.html
        nav = doc[doc.index('<nav class="workspace-nav"'):doc.index('</nav>', doc.index('<nav class="workspace-nav"'))]
        self.assertIn('id="studioStyleTab" data-workspace-target="layout"', nav)
        self.assertIn('id="studioEffectsTab" data-workspace-target="effects"', nav)
        self.assertEqual(nav.count('data-workspace-target='), 6)
        self.assertIn("const visibleWorkspace=name==='effects'?'layout':name", doc)
        self.assertIn('document.body.dataset.workspaceTask=name;', doc)
        self.assertIn("setHsrStudioMode(name==='effects'?'effects':'style')", doc)
        self.assertIn("previousWorkspace!=='layout'", doc)
        self.assertNotIn("addEventListener('click',()=>setHsrStudioMode('effects'))", doc)
        self.assertNotIn('class="hsr-studio-tabs"', doc)
        self.assertIn('data-studio-scope="style"', doc)
        self.assertIn('body[data-workspace-task="effects"]', self.css)

    def test_corpus_browser_advanced_not_second_primary_sidebar(self):
        doc = self.html
        self.assertIn('id="subListView"', doc)
        self.assertIn('id="layoutCorpusRail"', doc)
        self.assertIn('id="layoutAuditDrawerToggle" aria-expanded="false"', doc)
        self.assertIn('id="layoutCorpusRail" aria-label="批量排版检查与问题字幕定位" aria-hidden="true" inert', doc)
        self.assertIn("workbench.classList.toggle('audit-open',!!open)", doc)
        self.assertIn("rail.toggleAttribute('inert',!open)", doc)
        self.assertIn("rail.contains(document.activeElement)", doc)
        self.assertIn(".preview-workbench:not(.audit-open) .layout-corpus-rail", self.css)

    def test_mobile_qa_has_one_open_state_and_no_orphan_toggle(self):
        doc = self.html
        self.assertNotIn("layoutCorpusMobileToggleBtn", doc)
        self.assertNotIn(".layout-corpus-rail:not(.is-open)", doc)
        self.assertIn('id="layoutAuditDrawerToggle" aria-expanded="false" aria-controls="layoutCorpusRail"', doc)
        self.assertIn("workbench.classList.toggle('audit-open',!!open)", doc)
        self.assertIn("rail.classList.toggle('is-open',!!open)", doc)
        self.assertIn("rail.toggleAttribute('inert',!open)", doc)
        self.assertIn("toggle.setAttribute('aria-expanded',open?'true':'false')", doc)

    def test_one_final_text_editor_and_trial_hidden_by_default(self):
        doc = self.html
        self.assertIn('id="subFinalEditor"', doc)
        self.assertIn('id="layoutTrialTextDetails"', doc)
        self.assertIn("手动试调字幕文本（不会修改项目字幕）", doc)
        self.assertIn('id="layoutCueReadOnlyText"', doc)
        self.assertNotIn('id="layoutCueFinalText"', doc)
        self.assertNotIn('id="layoutCueAudio"', doc)
        self.assertEqual(doc.count('<audio '), 1)
        self.assertIn("display.textContent=selected?state.draft", doc)
        self.assertIn('id="layoutOpenProofreadingBtn"', doc)
        self.assertIn('class="hsr-export-advanced"', doc)

    def test_single_new_css_authority_and_valid_css_boundary(self):
        self.assertEqual(self.html.count('href="/static/hsr-ui-convergence.css"'), 1)
        self.assertNotIn(r"\\n#subtitleLayoutPreviewCard", self.html)
        self.assertNotIn(r"\n#subtitleLayoutPreviewCard", self.html)
        self.assertIn("min-width:1361px", self.css)
        self.assertIn("max-width:759px", self.css)
        self.assertIn("max-width:1360px", self.css)

    def test_effects_closes_layout_qa_and_nav_identity_is_semantic(self):
        doc = self.html
        self.assertIn("if(next==='effects'){", doc)
        self.assertIn("document.activeElement===drawerToggle", doc)
        self.assertIn("const target=document.body.dataset.hsrStudioFocus==='true'", doc)
        self.assertIn("document.querySelector('[data-hsr-focus-task=\"effects\"]')", doc)
        self.assertIn("setHsrAuditDrawer(false);", doc)
        self.assertIn("if(root.querySelector('.preview-workbench.audit-open'))setHsrAuditDrawer(false);", doc)
        self.assertIn('#subtitleLayoutPreviewCard[data-studio-mode="effects"] .layout-corpus-rail', self.css)
        for task in ("review", "layout", "effects", "export", "update"):
            self.assertIn(f'.workspace-nav button[data-workspace-target="{task}"]', doc)
        self.assertNotIn(".workspace-nav button:nth-child", doc)
        self.assertNotIn(".workspace-nav button:nth-child", self.css)
        self.assertNotIn(".hsr-studio-tabs button", doc)

    def test_focused_task_viewport_and_gesture_ownership(self):
        doc, css = self.html, self.css
        self.assertEqual(doc.count('id="hsrStudioFocusToggle"'), 1)
        self.assertEqual(doc.count('id="hsrTouchSliderToggle"'), 1)
        self.assertIn("function setHsrStudioFocusMode(enabled)", doc)
        self.assertIn("if(visibleWorkspace==='layout'){", doc)
        self.assertIn("setHsrStudioFocusMode(true);", doc)
        self.assertIn("if(visibleWorkspace!=='layout')setHsrStudioFocusMode(false)", doc)
        self.assertIn("event.key==='Escape'&&document.body.dataset.hsrStudioFocus==='true'", doc)
        self.assertIn('data-hsr-focus-task="layout"', doc)
        self.assertIn('data-hsr-focus-task="effects"', doc)
        self.assertIn("function setHsrTouchSliderLock(locked)", doc)
        self.assertIn("for(const eventName of ['pointerup','pointercancel'])",doc)
        self.assertIn('body[data-hsr-studio-focus="true"]', css)
        self.assertIn('overflow:hidden!important;overscroll-behavior:none', css)
        self.assertIn('position:fixed!important;inset:0!important', css)

    def test_production_script_parse(self):
        if not __import__("shutil").which("node"):
            self.skipTest("Node not installed")
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", self.html, re.S)
        self.assertTrue(scripts)
        with tempfile.TemporaryDirectory() as td:
            for i, script in enumerate(scripts):
                target = Path(td) / f"block-{i}.js"
                target.write_text(script, encoding="utf-8")
                run = subprocess.run(["node", "--check", str(target)], capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()
