"""Touch-safety, viewport, and Karaoke onboarding contracts."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TouchWorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "app/static/hsr-ui-convergence.css").read_text(encoding="utf-8")

    def test_default_bounded_viewport_across_desktop_and_touch(self):
        self.assertIn('height:clamp(340px,calc(100dvh - 210px),880px)',self.css)
        self.assertIn('height:max(340px,calc(100dvh - 196px))',self.css)
        self.assertIn('overscroll-behavior-y:contain',self.css)
        self.assertIn('grid-template-columns',self.css)
        self.assertNotIn('body[data-hsr-studio-focus="true"]',self.css)
        self.assertNotIn('setHsrStudioFocusMode(true)',self.html)

    def test_touch_numeric_editors_are_direct_not_unlocked_ranges(self):
        html, css = self.html, self.css
        self.assertNotIn('id="hsrTouchSliderToggle"',html)
        self.assertNotIn('setHsrTouchSliderLock',html)
        self.assertNotIn('.hsr-touch-sliders-unlocked',css)
        self.assertIn('function initHsrTouchNumericEditors()',html)
        self.assertIn("range.insertAdjacentElement('afterend',stepper)",html)
        self.assertIn("stepper.append(minus,editor,plus)",html)
        self.assertIn("editor.addEventListener('change'",html)
        self.assertIn("editor.dispatchEvent(new Event('change',{bubbles:true}))",html)
        self.assertIn("touch-action:manipulation",css)
        self.assertIn(".layout-timeline-navigation input[type=\"range\"]{",css)
        self.assertIn("display:none!important;",css)
        self.assertIn('@media(pointer:coarse)',css)

    def test_task_switch_does_not_reopen_every_long_accordion(self):
        self.assertIn("const previous=root.dataset.lastInspectorMode;", self.html)
        self.assertIn("section.open=first;", self.html)
        self.assertIn("root.dataset.lastInspectorMode=next;", self.html)

    def test_inspector_sections_are_exclusive_without_long_persistent_scroll(self):
        self.assertEqual(self.html.count('name="hsr-style-parameter-groups"'),4)
        self.assertEqual(self.html.count('name="hsr-effects-parameter-groups"'),3)
        self.assertIn('grid-template-rows:minmax(0,44%) minmax(0,56%)',self.css)

    def test_karaoke_input_is_explained_without_invented_alignment(self):
        self.assertIn('id="hsrAlignmentHelp"', self.html)
        self.assertIn('id="hsrWordAlignmentExample"', self.html)
        self.assertIn("本地生成缺失时间", self.html)
        self.assertIn("WhisperX", self.html)
        self.assertIn("不能直接导入任意项目", self.html)

    def test_karaoke_and_effect_feedback_have_visible_primary_entrypoints(self):
        html, css = self.html, self.css
        for name in ("wordAlignmentStatus", "refreshWordAlignmentBtn",
                     "generateWordAlignmentBtn", "importWordAlignmentBtn",
                     "wordAlignmentFile", "wordAlignmentProviderStatus",
                     "hsrEffectOperationStatus", "hsrEffectOperationTitle",
                     "hsrEffectOperationDetail", "hsrKaraokeReadiness"):
            self.assertEqual(html.count('id="' + name + '"'), 1, name)
        self.assertLess(html.index('id="hsrKaraokeReadiness"'),
                        html.index('class="hsr-footage-picker"'))
        self.assertLess(html.index('id="hsrEffectOperationStatus"'),
                        html.index('class="layout-preview-evidence"'))
        self.assertIn("function setHsrEffectOperation(phase, detail='')", html)
        self.assertIn("setHsrEffectOperation('encoding'", html)
        self.assertIn("setHsrEffectOperation('decoding'", html)
        self.assertIn("setHsrEffectOperation('ready'", html)
        self.assertIn("setHsrEffectOperation('failed'", html)
        self.assertIn("setHsrKaraokeReadiness('0 / '", html)
        self.assertIn(".hsr-karaoke-readiness",css)
        self.assertIn(".hsr-effect-operation-status",css)

    def test_playback_is_revealed_after_decode_not_before(self):
        a = self.html.index("async function renderHsrEffectVideo()")
        end = self.html.find("\nfunction ", a + 10)
        fragment = self.html[a:end if end >= 0 else None]
        self.assertLess(fragment.index("video.classList.remove('hidden')"),fragment.index("video.scrollIntoView"))
        self.assertIn("status.className='small status-bad'", fragment)


if __name__ == "__main__":
    unittest.main()
