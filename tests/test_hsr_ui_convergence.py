"""Regression contract for HSR subtitle UI task separation and reduced duplicate controls."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "app/static/index.html"
CSS = ROOT / "app/static/studio-v4.css"
SHARED_CSS = ROOT / "app/static/hsr-ui-convergence.css"


class HsrUiConvergence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")
        cls.css = CSS.read_text(encoding="utf-8")
        cls.shared = SHARED_CSS.read_text(encoding="utf-8")

    def test_task_navigation_outside_parameter_inspector(self):
        doc = self.html
        self.assertEqual(doc.count('id="studioStyleTab"'), 1)
        self.assertEqual(doc.count('id="studioEffectsTab"'), 1)
        self.assertLess(doc.index('id="studioStyleTab"'), doc.index('class="preview-workbench studio-v3-workbench studio-v4-workbench"'))
        self.assertLess(doc.index('id="studioEffectsTab"'), doc.index('class="preview-workbench studio-v3-workbench studio-v4-workbench"'))
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
        self.assertIn('body[data-workspace-task="effects"]', self.shared)

    def test_corpus_browser_advanced_not_second_primary_sidebar(self):
        doc = self.html
        self.assertIn('id="subListView"', doc)
        self.assertIn('id="layoutCorpusRail"', doc)
        self.assertIn('id="layoutAuditDrawerToggle" aria-expanded="false"', doc)
        self.assertIn('id="layoutCorpusRail" aria-label="项目字幕浏览器" aria-hidden="true" inert', doc)
        self.assertIn("workbench.classList.toggle('audit-open',!!open)", doc)
        self.assertIn("rail.toggleAttribute('inert',!open)", doc)
        self.assertIn("rail.contains(document.activeElement)", doc)
        self.assertIn('.studio-v4-workbench.audit-open>.layout-corpus-rail', self.css)

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
        self.assertEqual(self.html.count('href="/static/studio-v4.css"'), 1)
        self.assertIn("@media(max-width:1020px)", self.css)
        self.assertIn("@media(max-width:600px)", self.css)
        self.assertIn("studio-v3-command-bar", self.css)
        self.assertNotIn("grid-template-columns:minmax(0,1fr) clamp(390px", self.css)

    def test_effects_closes_layout_qa_and_nav_identity_is_semantic(self):
        doc = self.html
        self.assertIn("if(next==='effects'&&layoutCorpusFilter!=='all'){", doc)
        self.assertIn("if(sharedRail?.contains(document.activeElement))", doc)
        self.assertIn("document.getElementById('layoutAuditDrawerToggle')?.focus();", doc)
        self.assertNotIn("data-hsr-focus-task", doc)
        self.assertIn("setHsrAuditDrawer(false);", doc)
        self.assertIn("root.dataset.studioMode=next;", doc)
        self.assertIn('#subtitleLayoutPreviewCard[data-studio-mode="effects"] .layout-corpus-rail', self.shared)
        for task in ("review", "layout", "effects", "export", "update"):
            self.assertIn(f'.workspace-nav button[data-workspace-target="{task}"]', doc)
        self.assertNotIn(".workspace-nav button:nth-child", doc)
        self.assertNotIn(".workspace-nav button:nth-child", self.css)
        self.assertNotIn(".hsr-studio-tabs button", doc)

    def test_bounded_workbench_without_extra_focus_or_slider_lock(self):
        doc, css = self.html, self.css
        for control in ('hsrStudioFocusToggle','hsrTouchSliderToggle',
                        'hsrTouchSliderHint','hsrStudioFocusTasks',
                        'hsrFontImportShortcut'):
            self.assertNotIn('id="'+control+'"',doc)
        self.assertNotIn('data-hsr-focus-task=',doc)
        self.assertNotIn('setHsrStudioFocusMode(',doc)
        self.assertNotIn('setHsrTouchSliderLock(',doc)
        self.assertNotIn('data-hsr-studio-focus=',css)
        self.assertIn('body[data-workspace="layout"] #subtitleLayoutPreviewCard .studio-v4-workbench{',css)
        self.assertIn('height:auto!important',css)
        self.assertIn('@media(max-width:600px)',css)
        self.assertIn('overflow:visible!important',css)
        self.assertIn('id="projectFontImporter"',doc)
        self.assertIn('function initHsrTouchNumericEditors()',doc)
        self.assertIn("editor.type='number'",doc)
        self.assertIn("range.dispatchEvent(new Event('input',{bubbles:true}))",doc)
        self.assertIn('input[type="range"]{display:none!important}',self.shared)
        self.assertIn("range.insertAdjacentElement('afterend',stepper)",doc)
        self.assertNotIn('name="hsr-effects-parameter-groups"',doc)
        self.assertNotIn('name="hsr-style-parameter-groups"',doc)
        self.assertIn('input[type="range"]{',self.shared)

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
