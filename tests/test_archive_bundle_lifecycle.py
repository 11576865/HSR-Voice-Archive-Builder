"""Regression contracts for source-aware full MKV regeneration and saved backgrounds."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.archive_bundle_lifecycle import (
    OUTPUT, RECEIPT, bundle_export_status, load_previous_bundle_image,
    previous_bundle_settings, record_bundle_publication,
)
from app.subtitle_export_status import record_subtitle_export

# Minimal PNG header is enough to prove persistence/identity; real FFmpeg
# media decoding is tested separately in test_archive_bundle_exporter.py.
PNG = b"\x89PNG\r\n\x1a\n" + b"not-a-decoded-image-for-source-state-testing"


class BundleLifecycleTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        (self.root / "manifest.json").write_text('{"entries":[]}', encoding="utf-8")
        (self.root / "HSR_Voice_Archive.ass").write_text("[Events]\n", encoding="utf-8")
        (self.root / "HSR_Voice_Archive.srt").write_text("1\n", encoding="utf-8")
        (self.root / "continuous.flac").write_bytes(b"fake-source-flac")
        record_subtitle_export(self.root, state="current", ass_required=True)
        (self.root / OUTPUT).write_bytes(b"prior verified mux file")

    def _record(self, image=None):
        record_bundle_publication(
            self.root, background_color="#143256", background_image=image,
            image_extension=".png" if image else None, include_fonts=False,
        )

    def test_current_receipt_and_background_survive_source_changes(self):
        self._record(PNG)
        self.assertEqual(bundle_export_status(self.root)["state"], "current")
        self.assertEqual(load_previous_bundle_image(self.root), PNG)
        image_setting = previous_bundle_settings(self.root)
        self.assertEqual(image_setting["background_color"], "#143256")
        self.assertTrue(image_setting["image_saved"])
        (self.root / "continuous.flac").write_bytes(b"different FLAC")
        status = bundle_export_status(self.root)
        self.assertEqual(status["state"], "stale")
        self.assertTrue(status["needs_rebuild"])
        self.assertEqual((self.root / OUTPUT).read_bytes(), b"prior verified mux file")
        self.assertEqual(load_previous_bundle_image(self.root), PNG)

    def test_subtitle_revision_marks_existing_mkv_stale(self):
        self._record()
        (self.root / "subtitle_timing_overrides.json").write_text(
            '{"entry":12}', encoding="utf-8"
        )
        self.assertEqual(bundle_export_status(self.root)["state"], "stale")
        self.assertTrue((self.root / OUTPUT).is_file())

    def test_no_or_malformed_receipt_does_not_claim_current(self):
        self.assertEqual(bundle_export_status(self.root)["state"], "unverified")
        (self.root / RECEIPT).write_text("{", encoding="utf-8")
        self.assertEqual(bundle_export_status(self.root)["state"], "unverified")
        (self.root / OUTPUT).unlink()
        self.assertEqual(bundle_export_status(self.root)["state"], "missing")

    def test_corrupted_gradient_receipt_fails_closed_without_breaking_project_status(self):
        self._record()
        receipt_path = self.root / RECEIPT
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["background"]["spec"]["mode"] = "unexpected-filter-mode"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        status = bundle_export_status(self.root)
        self.assertEqual(status["state"], "unverified")
        self.assertFalse(status["known"])
        self.assertTrue((self.root / OUTPUT).exists())

    def test_source_marker_detects_in_progress_build_without_erasing_old_mkv(self):
        self._record()
        (self.root / ".build_in_progress").touch()
        self.assertEqual(bundle_export_status(self.root)["state"], "stale")
        (self.root / ".build_in_progress").unlink()
        self.assertEqual(bundle_export_status(self.root)["state"], "current")

    def test_image_tamper_refuses_reuse_and_marks_previous_mkv_stale(self):
        self._record(PNG)
        receipt = json.loads((self.root / RECEIPT).read_text(encoding="utf-8"))
        image = self.root / receipt["background"]["image_name"]
        image.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "背景图片"):
            load_previous_bundle_image(self.root)
        self.assertEqual(bundle_export_status(self.root)["state"], "stale")

    def test_color_only_settings_have_no_reusable_image(self):
        self._record()
        settings = previous_bundle_settings(self.root)
        self.assertEqual(settings["background_kind"], "color")
        self.assertFalse(settings["image_saved"])
        with self.assertRaisesRegex(ValueError, "没有可复用"):
            load_previous_bundle_image(self.root)

    def test_changed_or_removed_mkv_marks_receipt_obsolete(self):
        self._record()
        (self.root / OUTPUT).write_bytes(b"externally modified MKV")
        self.assertEqual(bundle_export_status(self.root)["state"], "stale")
        (self.root / OUTPUT).unlink()
        self.assertEqual(bundle_export_status(self.root)["state"], "missing")

    def test_project_summary_and_both_servers_expose_rebuild_contract(self):
        repo = Path(__file__).resolve().parents[1]
        project = (repo / "app/project.py").read_text(encoding="utf-8")
        self.assertIn('bundle_health = bundle_export_status(output)', project)
        self.assertIn('"bundle_export": bundle_health', project)
        self.assertIn('freshness=bundle_health["state"]', project)
        self.assertIn('needs_rebuild=bundle_health["needs_rebuild"]', project)
        self.assertIn('role="legacy_independent_video"', project)
        pipeline = (repo / "app/pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn('(out_dir / "HSR_Voice_Archive_Bundle.mkv").unlink(missing_ok=True)', pipeline)
        for path in ("app/server.py", "app/lite_server.py"):
            code = (repo / path).read_text(encoding="utf-8")
            # Lite's body-size router now mentions this endpoint earlier.
            # Inspect its actual handler, not the first URL string occurrence.
            anchor = ('if path == "/api/output/archive-bundle":' if path.endswith("lite_server.py")
                      else '@app.post("/api/output/archive-bundle")')
            pos = code.index(anchor)
            section = code[pos:pos + 1850]
            reuse_check = ('_bool(data.get("reuse_saved_background"))'
                           if path.endswith("lite_server.py")
                           else 'data.get("reuse_saved_background") is True')
            self.assertIn(reuse_check, section, path)
            self.assertIn('load_previous_bundle_image(output)', section, path)
        html = (repo / "app/static/index.html").read_text(encoding="utf-8")
        self.assertIn('id="bundleFreshnessStatus"', html)
        self.assertIn('id="bundleReuseBackground"', html)
        self.assertIn("reuse_saved_background:reuse", html)
        self.assertIn("bundleFormProjectRoot=null", html)


if __name__ == "__main__":
    unittest.main()
