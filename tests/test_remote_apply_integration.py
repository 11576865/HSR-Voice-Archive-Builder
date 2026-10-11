"""Synthetic end-to-end FastAPI remote Apply and guarded rollback, no network."""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.jobs import get_job
from app.project import PROJECT_FILENAME, create_project, load_project
from app.security import api_token
from app.server import _clear_active, _set_active, app


def _wav(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 1600)


class RemoteApplyLifecycleTests(unittest.TestCase):
    def setUp(self):
        _clear_active()
        self.addCleanup(_clear_active)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state_patch = patch("app.project.STATE_FILE", self.root / "global-state.json")
        self.jobs_patch = patch("app.jobs.JOBS_FILE", self.root / "global-jobs.json")
        self.state_patch.start()
        self.jobs_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.jobs_patch.stop)
        _wav(self.root / "wavs" / "chapter" / "old.wav")
        self.index = self.root / "index.csv"
        self.index.write_text(
            "index,group,filename,source_member_id,english\n"
            "1,Chapter,old.wav,chapter/old.wav,Hello\n",
            encoding="utf-8",
        )
        self.output = self.root / "output"
        self.output.mkdir()
        manifest = self.output / "manifest.json"
        manifest.write_text(
            json.dumps({"entries": [{
                "index": 1,
                "filename": "old.wav",
                "source_member_id": "chapter/old.wav",
                "source_text": "Hello",
            }]}),
            encoding="utf-8",
        )
        (self.output / "update_plan.json").write_text(json.dumps({
            "remote_rows": 2,
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "new_logical": [
                {"filename": "new.wav", "metadata": {
                    "filename": "new.wav", "english": "New voice",
                }},
            ],
        }), encoding="utf-8")
        self.config = create_project(
            self.root, name="SyntheticRemoteApply", index_csv="index.csv",
            wav_source="wavs", output_dir="output",
        )
        self.original_config_bytes = (self.root / PROJECT_FILENAME).read_bytes()
        self.original_index_bytes = self.index.read_bytes()
        _set_active(self.config)
        self.client = TestClient(app, base_url="http://127.0.0.1:8765")
        self.addCleanup(self.client.close)
        self.headers = {"X-HSR-Token": api_token()}

    def _provider(self):
        def fake_meta(result_json, progress, **kwargs):
            result_json.parent.mkdir(parents=True, exist_ok=True)
            result_json.write_text("{}", encoding="utf-8")
        def fake_resolve(_result_json, targets, **kwargs):
            self.assertEqual([row["filename"] for row in targets], ["new.wav"])
            return {"targets": {"new.wav": {"row_idx": 0}},
                    "reference_targets": {}, "unresolved": []}
        def fake_download(_plan, destination, progress, **kwargs):
            _wav(destination / "new.wav")
            return {"completed": [{"filename": "new.wav", "status": "ok"}],
                    "failed": [], "unresolved": []}
        return [
            patch("app.server.download_result_json", side_effect=fake_meta),
            patch("app.server.resolve_targets", side_effect=fake_resolve),
            patch("app.server.download_resolved_audio", side_effect=fake_download),
            patch("app.server.confirmed_reference_metadata", return_value={}),
        ]

    def _job_result(self, job_id: str):
        for _ in range(120):
            job = get_job(job_id)
            if job and job.get("state") in {"succeeded", "failed", "interrupted"}:
                return job
            time.sleep(0.04)
        self.fail(f"Job did not complete: {job_id}")

    def test_apply_publishes_version_and_rollback_retains_old_audio_and_index(self):
        patches = self._provider()
        for item in patches:
            item.start()
        try:
            response = self.client.post(
                "/api/update/apply-remote",
                data={"selected_filenames": '["new.wav"]'},
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200, response.text)
            result = self._job_result(response.json()["job"])
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual(result["state"], "succeeded", result.get("error"))
        self.assertTrue(result["result"]["project_updated"])
        cfg = load_project(self.root)
        published_source = self.root / cfg.wav_source
        published_index = self.root / cfg.index_csv
        self.assertTrue((published_source / "chapter" / "old.wav").is_file())
        self.assertTrue((published_source / "new.wav").is_file())
        self.assertIn("new.wav,new.wav", published_index.read_text(encoding="utf-8-sig"))
        self.assertEqual(self.index.read_bytes(), self.original_index_bytes)
        self.assertTrue((self.root / "wavs" / "chapter" / "old.wav").is_file())

        response = self.client.get("/api/update/revisions", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        records = response.json()["revisions"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["state"], "active")
        self.assertTrue(records[0]["can_rollback"])
        rollback = self.client.post("/api/update/rollback", data={
            "revision_id": records[0]["revision_id"],
            "expected_project_sha256": records[0]["current_project_sha256"],
        }, headers=self.headers)
        self.assertEqual(rollback.status_code, 200, rollback.text)
        restored_job = self._job_result(rollback.json()["job"])
        self.assertEqual(restored_job["state"], "succeeded", restored_job.get("error"))
        self.assertEqual((self.root / PROJECT_FILENAME).read_bytes(),
                         self.original_config_bytes)
        self.assertEqual(load_project(self.root).wav_source, "wavs")
        self.assertTrue((published_source / "new.wav").exists())
        self.assertTrue(published_index.exists())
        status = self.client.get("/api/update/revisions", headers=self.headers).json()
        self.assertEqual(status["revisions"][0]["state"], "rolled_back")
        self.assertFalse(status["revisions"][0]["can_rollback"])

    def test_publish_failure_does_not_redirect_existing_project(self):
        patches = self._provider()
        patches.append(patch(
            "app.server.publish_remote_revision",
            side_effect=OSError("injected commit failure"),
        ))
        for item in patches:
            item.start()
        try:
            response = self.client.post(
                "/api/update/apply-remote",
                data={"selected_filenames": '["new.wav"]'},
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200, response.text)
            failed = self._job_result(response.json()["job"])
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual(failed["state"], "failed")
        self.assertIn("injected commit failure", failed.get("error", ""))
        self.assertEqual((self.root / PROJECT_FILENAME).read_bytes(),
                         self.original_config_bytes)
        self.assertEqual(load_project(self.root).index_csv, "index.csv")
        self.assertEqual(self.index.read_bytes(), self.original_index_bytes)


if __name__ == "__main__":
    unittest.main()
