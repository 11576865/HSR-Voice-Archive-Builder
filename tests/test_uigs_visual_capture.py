from __future__ import annotations
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class UigsVisualCaptureContractTest(unittest.TestCase):
    def test_covers_all_declared_hsr_surfaces(self):
        contract = json.loads((ROOT / ".uigs" / "ui-visual-capture.json").read_text(encoding="utf-8"))
        inventory = json.loads((ROOT / ".uigs" / "ui-surfaces.json").read_text(encoding="utf-8"))
        captured = {sid for capture in contract["captures"] for sid in capture["surface_ids"]}
        declared = {surface["id"] for surface in inventory["surfaces"]}
        self.assertEqual(captured, declared)

    def test_workspace_selectors_are_stable_production_ids(self):
        contract = json.loads((ROOT / ".uigs" / "ui-visual-capture.json").read_text(encoding="utf-8"))
        by_id = {capture["id"]: capture for capture in contract["captures"]}
        self.assertEqual(by_id["HSR.REVIEW.WORKBENCH.DESKTOP_DARK"]["selector"], "#subtitleReviewCard")
        self.assertEqual(by_id["HSR.LAYOUT.WORKBENCH.DESKTOP_DARK"]["selector"], "#subtitleLayoutPreviewCard")
        self.assertEqual(by_id["HSR.EXPORT.WORKSPACE.DESKTOP_DARK"]["selector"], "#gptSovitsCard")
        self.assertEqual(by_id["HSR.UPDATE.WORKSPACE.DESKTOP_DARK"]["selector"], "#updateCard")

if __name__ == "__main__":
    unittest.main()
