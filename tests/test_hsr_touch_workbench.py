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

    def test_viewport_bounded_on_desktop_and_touch(self):
        self.assertIn("height:clamp(440px,calc(100dvh - 225px),1100px)", self.css)
        self.assertIn("overflow-y:auto!important", self.css)
        self.assertIn("overscroll-behavior-y:contain", self.css)
        self.assertIn("max-height:min(70dvh,720px)", self.css)
        self.assertIn("grid-template-columns", self.css)

    def test_touch_slider_is_explicitly_armed_and_accessible(self):
        html, css = self.html, self.css
        self.assertIn('id="hsrTouchSliderToggle" type="button" aria-pressed="false"', html)
        self.assertIn("inspector.classList.toggle('hsr-touch-sliders-unlocked')", html)
        self.assertIn("toggle.setAttribute('aria-pressed'", html)
        self.assertIn(".preview-inspector:not(.hsr-touch-sliders-unlocked) input[type=\"range\"]", css)
        self.assertIn("pointer-events:none!important", css)
        self.assertIn("@media (pointer:coarse)", css)
        self.assertIn("touch-action:pan-y", css)

    def test_task_switch_does_not_reopen_every_long_accordion(self):
        self.assertIn("const previous=root.dataset.lastInspectorMode;", self.html)
        self.assertIn("section.open=first;", self.html)
        self.assertIn("root.dataset.lastInspectorMode=next;", self.html)

    def test_karaoke_input_is_explained_without_invented_alignment(self):
        self.assertIn('id="hsrAlignmentHelp"', self.html)
        self.assertIn('id="hsrWordAlignmentExample"', self.html)
        self.assertIn("本地生成缺失时间", self.html)
        self.assertIn("WhisperX", self.html)
        self.assertIn("不能直接导入任意项目", self.html)

    def test_playback_is_revealed_after_decode_not_before(self):
        a = self.html.index("async function renderHsrEffectVideo()")
        end = self.html.find("\nfunction ", a + 10)
        fragment = self.html[a:end if end >= 0 else None]
        self.assertLess(fragment.index("video.classList.remove('hidden')"),fragment.index("video.scrollIntoView"))
        self.assertIn("status.className='small status-bad'", fragment)


if __name__ == "__main__":
    unittest.main()
