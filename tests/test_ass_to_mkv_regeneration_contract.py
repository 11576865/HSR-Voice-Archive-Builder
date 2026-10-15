"""Every complete-MKV ASS generation entry point must have one publishing job."""
from __future__ import annotations

import unittest
from pathlib import Path


class AssToMkvWorkflowContracts(unittest.TestCase):
    def test_fastapi_and_lite_both_use_the_same_full_delivery_job(self):
        root = Path(__file__).resolve().parents[1]
        service = (root / "app/auto_delivery.py").read_text(encoding="utf-8")
        self.assertIn("def queue_ass_regeneration(", service)
        self.assertIn("publish_generated_ass_complete_mkv(", service)
        self.assertIn('"delivery": "complete-mkv"', service)
        self.assertIn('raise ValueError("ASS 生成失败，MKV 未更新："', service)

        for name in ("app/server.py", "app/lite_server.py"):
            backend = (root / name).read_text(encoding="utf-8")
            self.assertIn("queue_ass_regeneration(config, output)", backend)
            if name == "app/server.py":
                self.assertIn("queue_ass_regeneration(config, output_dir)", backend)
            self.assertIn('"delivery": "complete-mkv"', backend)

    def test_browser_automatic_ass_refresh_waits_for_mkv_job(self):
        html = (Path(__file__).resolve().parents[1] /
                "app/static/index.html").read_text(encoding="utf-8")
        start = html.index("async function runAutoAssRefresh(")
        end = html.index("function queueSubtitleSettingsAutosave(", start)
        body = html[start:end]
        self.assertIn("if(result.job)", body)
        self.assertIn("watchJob(result.job)", body)
        self.assertIn("将新 ASS 封入 MKV", body)

    def test_mkv_tracks_are_compared_semantically_before_atomic_replace(self):
        exporter = (Path(__file__).resolve().parents[1] /
                    "app/archive_bundle_exporter.py").read_text(encoding="utf-8")
        verify = exporter.index("self._verify_embedded_ass(temporary, subtitles)")
        publish = exporter.index("temporary.replace(target)", verify)
        self.assertLess(verify, publish)


if __name__ == "__main__":
    unittest.main()
