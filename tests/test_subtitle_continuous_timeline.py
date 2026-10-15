"""Source-level guard: one shared subtitle browser, no redundant Effects timeline.

The previous Node test instantiated synthetic DOM nodes for a timeline even when
that UI was absent. This regression now asserts the delivered DOM and handler
ownership instead of giving a removed UI a false positive.
"""
from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SubtitleCueBrowserOwnershipTests(unittest.TestCase):
    def test_shared_cue_browser_owns_selection_in_both_tasks(self):
        html=(ROOT/"app/static/index.html").read_text("utf-8")
        css=(ROOT/"app/static/hsr-ui-convergence.css").read_text("utf-8")
        for name in ("layoutTimelinePanel","layoutTimelineTracks",
                     "studioV3TimelineTab","layoutTimelineViewport"):
            self.assertNotIn(f'id="{name}"',html)
        for name in ("layoutCorpusRail","layoutCorpusList","layoutCorpusSearch",
                     "layoutAuditDrawerToggle","layoutCorpusPrevPageBtn",
                     "layoutCorpusNextPageBtn","layoutCorpusPageStatus",
                     "layoutCueClockOrigin"):
            self.assertEqual(html.count(f'id="{name}"'),1,name)
        self.assertIn("setStudioV3Pane(next==='effects'?'cue':'inspector')",html)
        self.assertIn("function renderLayoutCorpusRail()",html)
        self.assertIn("function selectLayoutCorpusByIndex(index)",html)
        self.assertIn("loadLayoutStressSample({force:true,selectId:sub.id})",html)
        self.assertIn("function setHsrAuditDrawer(open)",html)
        self.assertIn("setHsrAuditDrawer(false);",html)
        self.assertIn('[data-studio-mode="effects"] #layoutAuditBtn',css)
        self.assertNotIn('[data-studio-mode="effects"] .layout-corpus-rail{display:none!important}',css)
        # The audio-derived clock remains immutable; cue selection does not mutate it.
        self.assertNotIn('id="layoutCueTimingSaveBtn"',html)
        self.assertNotIn('data-timeline-edge="start"',html)


if __name__=="__main__":
    unittest.main()
