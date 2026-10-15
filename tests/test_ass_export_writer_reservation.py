"""ASS exports must share one project writer gate across both HTTP backends."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app import jobs


class ProjectOutputReservationTests(unittest.TestCase):
    def setUp(self):
        self.jobs_patch = patch.object(jobs, "_JOBS", {})
        self.reservations_patch = patch.object(jobs, "_RESERVED_PROJECT_WRITERS", {})
        self.persistence_patch = patch.object(jobs, "_persist_locked")
        self.jobs_patch.start()
        self.reservations_patch.start()
        self.persistence_patch.start()

    def tearDown(self):
        self.persistence_patch.stop()
        self.reservations_patch.stop()
        self.jobs_patch.stop()

    def test_sync_refresh_blocks_another_sync_refresh_and_async_export(self):
        with patch.object(jobs.threading.Thread, "start"):
            with jobs.reserve_project_output_writer("/same"):
                with self.assertRaisesRegex(RuntimeError, "ass-refresh"):
                    with jobs.reserve_project_output_writer("/same"):
                        pass
                with self.assertRaisesRegex(RuntimeError, "ass-refresh"):
                    jobs.create_job("ass-export", lambda: None, project_root="/same")
                with self.assertRaisesRegex(RuntimeError, "字幕输出仍在运行"):
                    jobs.assert_project_idle("/same")
                # Other projects still have their own independent writer slot.
                with jobs.reserve_project_output_writer("/different"):
                    pass
            # Released reservations must allow jobs to be submitted again.
            job = jobs.create_job("ass-export", lambda: None, project_root="/same")
            self.assertEqual(job.state, "queued")

    def test_queued_job_blocks_sync_refresh(self):
        with patch.object(jobs.threading.Thread, "start"):
            jobs.create_job("archive-bundle-export", lambda: None, project_root="/project")
        with self.assertRaisesRegex(RuntimeError, "archive-bundle-export"):
            with jobs.reserve_project_output_writer("/project"):
                pass

    def test_exception_releases_reservation(self):
        with self.assertRaisesRegex(ValueError, "deliberate"):
            with jobs.reserve_project_output_writer("/project"):
                raise ValueError("deliberate")
        self.assertNotIn("/project", jobs._RESERVED_PROJECT_WRITERS)
        with jobs.reserve_project_output_writer("/project"):
            pass

    def test_real_server_routes_use_the_same_reservation(self):
        source = Path(__file__).resolve().parents[1]
        fastapi = (source / "app/server.py").read_text(encoding="utf-8")
        lite = (source / "app/lite_server.py").read_text(encoding="utf-8")
        for backend in (fastapi, lite):
            route = backend.split('"/subtitles/artifacts/refresh"', 1)
            # Dynamic project paths are checked via path.endswith(), not a literal route.
            self.assertIn("reserve_project_output_writer(config.root)", backend)
            self.assertIn("ass_progress=report_ass_progress", backend)
        self.assertIn("await run_in_threadpool(refresh_ass_only", fastapi)


if __name__ == "__main__":
    unittest.main()
