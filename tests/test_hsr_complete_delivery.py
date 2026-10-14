"""Default delivery contract, explicit FLAC-only alternative and safe job dedupe."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.auto_delivery import _ATTEMPTS, maybe_queue_complete_mkv
from app.project import ProjectConfig


class AutoCompleteDeliveryTests(unittest.TestCase):
    def setUp(self):
        _ATTEMPTS.clear()

    def _config(self, root: Path, mode: str = "complete"):
        return SimpleNamespace(delivery_mode=mode, root=str(root), name="Dahlia",
                               output_dir="output")

    def test_project_profile_defaults_to_full_mkv_and_both_subtitles(self):
        # Requiring ProjectConfig avoids a fragile static HTML-only contract.
        values = ProjectConfig(schema_version=1, name="Dahlia", root="/dahlia",
                               index_csv="index.csv", wav_source="source")
        self.assertEqual(values.delivery_mode, "complete")
        self.assertTrue(values.generate_ass)
        self.assertTrue(values.make_flac)

    def test_only_flac_profile_never_attempts_video_regeneration(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.auto_delivery.bundle_export_status") as status:
                self.assertIsNone(maybe_queue_complete_mkv(
                    self._config(Path(tmp), "flac_only")))
                status.assert_not_called()

    def test_same_source_generation_queues_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._config(Path(tmp))
            ready = {"needs_rebuild": True, "can_generate": True}
            settings = {"attach_project_fonts": False, "background_spec": {
                "mode": "color", "resolution": "720p", "quality": "standard",
            }, "background_color": "#000000"}
            with (patch("app.auto_delivery.resolve_project_path", return_value=Path(tmp)),
                  patch("app.auto_delivery.bundle_export_status", return_value=ready),
                  patch("app.auto_delivery.previous_bundle_settings", return_value=settings),
                  patch("app.auto_delivery._input_identity", return_value="source-a"),
                  patch("app.auto_delivery.create_job", return_value=SimpleNamespace(id="job-a")) as create):
                self.assertEqual(maybe_queue_complete_mkv(project), "job-a")
                self.assertIsNone(maybe_queue_complete_mkv(project))
                create.assert_called_once()
            with (patch("app.auto_delivery.resolve_project_path", return_value=Path(tmp)),
                  patch("app.auto_delivery.bundle_export_status", return_value=ready),
                  patch("app.auto_delivery.previous_bundle_settings", return_value=settings),
                  patch("app.auto_delivery._input_identity", return_value="source-b"),
                  patch("app.auto_delivery.create_job", return_value=SimpleNamespace(id="job-b"))):
                self.assertEqual(maybe_queue_complete_mkv(project), "job-b")

    def test_failed_auto_mkv_is_retried_after_backoff_not_forever_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._config(Path(tmp))
            ready = {"needs_rebuild": True, "can_generate": True}
            settings = {"attach_project_fonts": False, "background_spec": {
                "mode": "color", "resolution": "720p", "quality": "standard",
            }, "background_color": "#000000"}
            from app.auto_delivery import RETRY_BASE_SECONDS
            _ATTEMPTS[project.root] = ("same-source", "failed-job", 100.0, 0)
            with (patch("app.auto_delivery.resolve_project_path", return_value=Path(tmp)),
                  patch("app.auto_delivery.bundle_export_status", return_value=ready),
                  patch("app.auto_delivery.previous_bundle_settings", return_value=settings),
                  patch("app.auto_delivery._input_identity", return_value="same-source"),
                  patch("app.auto_delivery.get_job", return_value={"state": "failed"}),
                  patch("app.auto_delivery.create_job", return_value=SimpleNamespace(id="retry-job")) as queued,
                  patch("app.auto_delivery.time.monotonic", return_value=100.0 + RETRY_BASE_SECONDS - 1)):
                self.assertIsNone(maybe_queue_complete_mkv(project))
                queued.assert_not_called()
            with (patch("app.auto_delivery.resolve_project_path", return_value=Path(tmp)),
                  patch("app.auto_delivery.bundle_export_status", return_value=ready),
                  patch("app.auto_delivery.previous_bundle_settings", return_value=settings),
                  patch("app.auto_delivery._input_identity", return_value="same-source"),
                  patch("app.auto_delivery.get_job", return_value={"state": "failed"}),
                  patch("app.auto_delivery.create_job", return_value=SimpleNamespace(id="retry-job")) as queued,
                  patch("app.auto_delivery.time.monotonic", return_value=100.0 + RETRY_BASE_SECONDS + 1)):
                self.assertEqual(maybe_queue_complete_mkv(project), "retry-job")
                queued.assert_called_once()
            self.assertEqual(_ATTEMPTS[project.root][1], "retry-job")
            self.assertEqual(_ATTEMPTS[project.root][3], 1)

    def test_running_auto_mkv_does_not_duplicate_on_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._config(Path(tmp))
            _ATTEMPTS[project.root] = ("same", "running-job", 100.0, 0)
            with (patch("app.auto_delivery.resolve_project_path", return_value=Path(tmp)),
                  patch("app.auto_delivery.bundle_export_status", return_value={
                      "needs_rebuild": True, "can_generate": True}),
                  patch("app.auto_delivery.previous_bundle_settings", return_value={
                      "attach_project_fonts": False, "background_spec": {}, "background_color": "#000"}),
                  patch("app.auto_delivery._input_identity", return_value="same"),
                  patch("app.auto_delivery.get_job", return_value={"state": "running"}),
                  patch("app.auto_delivery.create_job") as queued):
                self.assertIsNone(maybe_queue_complete_mkv(project))
                queued.assert_not_called()

    def test_dirty_or_building_sources_do_not_trigger_video_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._config(Path(tmp))
            with (patch("app.auto_delivery.resolve_project_path", return_value=Path(tmp)),
                  patch("app.auto_delivery.bundle_export_status",
                        return_value={"needs_rebuild": True, "can_generate": False}),
                  patch("app.auto_delivery.create_job") as create):
                self.assertIsNone(maybe_queue_complete_mkv(project))
                create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
