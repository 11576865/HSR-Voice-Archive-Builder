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
        cls.css = ((ROOT / "app/static/hsr-ui-convergence.css").read_text(encoding="utf-8") + "\n" +
                   (ROOT / "app/static/studio-v3.css").read_text(encoding="utf-8"))

    def test_default_bounded_viewport_across_desktop_and_touch(self):
        self.assertIn('height:clamp(470px,calc(100dvh - 184px),1020px)',self.css)
        self.assertIn('height:max(420px,calc(100dvh - 168px))',self.css)
        self.assertIn('overscroll-behavior:contain',self.css)
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

    def test_long_press_and_bounds_have_explicit_contracts(self):
        html=self.html
        self.assertIn("dwellTimer=setTimeout(()=>{",html)
        self.assertIn("repeatTimer=setInterval(stepOnce,90)",html)
        self.assertIn("if(suppressClick){suppressClick=false;return;}",html)
        self.assertIn("'pointerup','pointercancel','pointerleave','lostpointercapture'",html)
        self.assertIn("window.addEventListener('blur',stopRepeat)",html)
        self.assertIn("if(button.disabled)stopRepeat()",html)
        for field, maximum in (
            ("prevOutlineWidth","12"), ("prevShadowDepth","12"),
            ("prevBlurRadius","5")):
            self.assertIn(f'id="{field}" min="0" max="{maximum}"',html)
        self.assertIn("editor.reportValidity()",html)
        self.assertIn("并非 ASS 格式上限",html)

    def test_geometry_appearance_is_feedback_not_evidence(self):
        html=self.html
        self.assertIn("const appearanceGeometryPreviewSpec=()=>",html)
        self.assertIn("const appearance=appearanceGeometryPreviewSpec()",html)
        self.assertNotIn("elements.push(geometryStyledText(",html)
        self.assertIn("辅助边界图（无文字渲染）",html)
        self.assertIn("if(appearance.card)elements.push(geometryCard(bbox))",html)
        self.assertIn("hsr-geometry-style-blur",html)
        # Assert the non-authoritative visual contract, not a legacy
        # literal sentence that was intentionally rewritten in PR #179.
        self.assertIn("geometry:['辅助几何图（非排版成品）'",html)
        self.assertIn("未经真实验证",html)
        self.assertIn("不要依据此图定稿；等待自动真实渲染",html)
        # Real frame failures must not suggest a geometrically fake glyph
        # substitute. Assert the fail-closed evidence state rather than old copy.
        self.assertIn("真实渲染失败 · 不可定稿",html)
        self.assertIn("const realFrame=!(mode==='geometry'&&geometryExplicitlyRequested)",html)
        self.assertIn("geometryExplicitlyRequested=true;",html)

    def test_task_switch_does_not_reopen_every_long_accordion(self):
        self.assertIn("const previous=root.dataset.lastInspectorMode;", self.html)
        self.assertIn("section.open=next==='style'?visibleIndex<2:visibleIndex===0", self.html)
        self.assertIn("root.dataset.lastInspectorMode=next;", self.html)

    def test_inspector_sections_are_exclusive_without_long_persistent_scroll(self):
        self.assertNotIn('name="hsr-style-parameter-groups"',self.html)
        self.assertNotIn('name="hsr-effects-parameter-groups"',self.html)
        self.assertIn('height:auto!important',self.css)

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
