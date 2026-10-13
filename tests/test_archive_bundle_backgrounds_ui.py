"""Contracts for one HSR MKV background editor and two matching APIs."""
from __future__ import annotations

import unittest
from pathlib import Path


class ArchiveBackgroundUiContractTests(unittest.TestCase):
    def test_single_export_editor_exposes_all_background_choices(self):
        source = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text(encoding="utf-8")
        for id in ("bundleBackgroundMode", "bundleGradientFirst",
                   "bundleGradientSecond", "bundleGradientThird",
                   "bundleGradientDirection", "bundleVideoResolution",
                   "bundleVideoQuality", "bundleBackgroundPreview",
                   "bundlePreviewBtn", "bundlePreviewStatus"):
            with self.subTest(id=id):
                self.assertEqual(source.count('id="' + id + '"'), 1)
        for mode in ("color", "linear", "radial", "soft", "image"):
            self.assertIn('<option value="' + mode + '">', source)
        for quality in ("standard", "high"):
            self.assertIn('<option value="' + quality + '">', source)
        self.assertIn("function bundleBackgroundSpec()", source)
        self.assertIn("function previewBundleBackground()", source)
        self.assertIn("'data:image/png;base64,'+response.image_base64", source)
        self.assertIn("snapshot!==JSON.stringify(bundleBackgroundSpec())", source)
        self.assertIn("background_spec:spec", source)
        self.assertIn("'bundleVideoResolution'", source)
        self.assertIn("syncArchiveBundleProjectState(project,force=false)", source)

    def test_full_and_lite_use_same_validated_source_preview_and_export(self):
        repo = Path(__file__).resolve().parents[1]
        for path in ("app/server.py", "app/lite_server.py"):
            source = (repo / path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn('"/api/output/archive-bundle/background-preview"', source)
                self.assertIn('"/api/output/archive-bundle"', source)
                self.assertEqual(source.count('normalize_bundle_background(raw_spec'), 2)
                self.assertIn('render_background_preview', source)
                self.assertIn('background_spec=spec', source)
                self.assertIn('RESOLUTIONS[spec["resolution"]][0]', source)
                self.assertIn('RESOLUTIONS[spec["resolution"]][1]', source)


if __name__ == "__main__":
    unittest.main()
