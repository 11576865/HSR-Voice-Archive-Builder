"""A fully exercised mocked-provider remote voice update, not just a publisher unit test."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.project import create_project, load_project, PROJECT_FILENAME
from app.remote_update_publish import list_remote_revisions
from app.security import api_token
from app.server import app, _set_active, _clear_active


def _small_pcm_wav() -> bytes:
    sink = io.BytesIO()
    with wave.open(sink, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 128)
    return sink.getvalue()


class CompleteIncrementalApplyTests(unittest.TestCase):
    def setUp(self):
        _clear_active()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch_state = patch("app.project.STATE_FILE", self.root / "state.json")
        self.patch_state.start()
        self.addCleanup(self.patch_state.stop)
        self.original = _small_pcm_wav()
        wavs = self.root / "wavs"
        for chapter in ("chapterA", "chapterB"):
            (wavs / chapter).mkdir(parents=True)
            (wavs / chapter / "voice.wav").write_bytes(self.original)
        index = self.root / "index.csv"
        index.write_text(
            "index,filename,source_member_id,english\n"
            "1,voice.wav,chapterA/voice.wav,First\n"
            "2,voice.wav,chapterB/voice.wav,Second\n",
            encoding="utf-8",
        )
        output = self.root / "output"
        output.mkdir()
        manifest = output / "manifest.json"
        manifest.write_text(
            json.dumps({"entries": [
                {"index": 1, "filename": "voice.wav",
                 "source_member_id": "chapterA/voice.wav", "source_text": "First"},
                {"index": 2, "filename": "voice.wav",
                 "source_member_id": "chapterB/voice.wav", "source_text": "Second"},
            ]}),
            encoding="utf-8",
        )
        plan = {
            "remote_rows": 1,
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "new_logical": [{
                "filename": "new.wav", "metadata": {"filename": "new.wav", "english": "New"},
            }],
        }
        (output / "update_plan.json").write_text(json.dumps(plan), encoding="utf-8")
        self.config = create_project(
            self.root, name="Integration fixture", index_csv="index.csv",
            wav_source="wavs", output_dir="output",
        )
        _set_active(self.config)
        self.before_config = (self.root / PROJECT_FILENAME).read_bytes()
        self.new_wav = _small_pcm_wav()
        self.jobs = []

    def tearDown(self):
        _clear_active()

    def _run(self, download_kind="normal"):
        def metadata(dest, callback, *, dataset):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("{}", encoding="utf-8")

        resolved = {
            "targets": {"new.wav": {"row_idx": 1}},
            "reference_targets": {}, "unresolved": [], "dataset": "test-dataset",
        }

        def download(plan, dest, callback, *, dataset):
            dest.mkdir(parents=True, exist_ok=True)
            filename = "surprise.wav" if download_kind == "extra" else "new.wav"
            result = dest / filename
            result.write_bytes(self.new_wav)
            return {
                "completed": [{
                    "filename": filename,
                    "path": str(result), "status": (
                        "exists" if download_kind == "cached" else "ok"
                    ),
                    "sha256": hashlib.sha256(self.new_wav).hexdigest(),
                }],
                "failed": [], "unresolved": [],
            }

        def immediately(kind, fn, *, with_progress=False, **kwargs):
            self.jobs.append(kind)
            fn(lambda *args, **kwargs: None)
            return SimpleNamespace(id="sync-integration-job")

        with patch("app.server.download_result_json", side_effect=metadata), \
             patch("app.server.resolve_targets", return_value=resolved), \
             patch("app.server.download_resolved_audio", side_effect=download), \
             patch("app.server.create_job", side_effect=immediately), \
             TestClient(app, base_url="http://127.0.0.1:8765") as client:
            return client.post(
                "/api/update/apply-remote",
                data={"selected_filenames": '["new.wav"]'},
                headers={"X-HSR-Token": api_token()},
            )

    def test_full_apply_merges_nested_old_wavs_and_only_verified_new_wav(self):
        response = self._run()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.jobs, ["remote-update-apply"])
        config = load_project(self.root)
        self.assertNotEqual((self.root / PROJECT_FILENAME).read_bytes(), self.before_config)
        source = self.root / config.wav_source
        index = self.root / config.index_csv
        self.assertTrue(source.is_dir())
        self.assertTrue(index.is_file())
        self.assertEqual((source / "chapterA/voice.wav").read_bytes(), self.original)
        self.assertEqual((source / "chapterB/voice.wav").read_bytes(), self.original)
        self.assertEqual((source / "new.wav").read_bytes(), self.new_wav)
        with index.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(
            [(r["filename"], r["source_member_id"]) for r in rows],
            [("voice.wav", "chapterA/voice.wav"),
             ("voice.wav", "chapterB/voice.wav"), ("new.wav", "new.wav")],
        )
        status = list_remote_revisions(config)
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["state"], "active")
        self.assertTrue(status[0]["can_rollback"])
        self.assertEqual(
            status[0]["new_audio_unchanged"], True,
        )
        reports = json.loads((self.root / "output/update_apply_report.json").read_text())
        self.assertEqual(reports["applied_filenames"], ["new.wav"])
        self.assertEqual(reports["revision_id"], status[0]["revision_id"])
        self.assertFalse(
            list((self.root / ".generated/incremental_download_sessions").glob(
                "session-*/source/new.wav"
            )),
            "Session WAV must not be reused as proof by the next Apply",
        )
        self.assertTrue((self.root / "wavs/chapterA/voice.wav").is_file())

    def test_extra_or_cached_provider_completion_never_switches_live_sources(self):
        for mode in ("extra", "cached"):
            with self.subTest(mode=mode):
                self.jobs.clear()
                response = self._run(download_kind=mode)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn(
                    "Unexpected completed" if mode == "extra" else "Unverified cached",
                    response.json()["error"],
                )
                self.assertEqual(
                    (self.root / PROJECT_FILENAME).read_bytes(), self.before_config,
                )
                self.assertEqual(load_project(self.root).wav_source, "wavs")
                self.assertEqual(list_remote_revisions(load_project(self.root)), [])
                self.assertFalse((self.root / "output/update_apply_report.json").exists())

    def test_fastapi_and_termux_same_verified_download_and_publish_contract(self):
        root = Path(__file__).resolve().parents[1]
        for file in ("server.py", "lite_server.py"):
            with self.subTest(file=file):
                code = (root / "app" / file).read_text(encoding="utf-8")
                self.assertIn(
                    "verified_completed_new_wavs(targets, downloaded, incoming)", code
                )
                self.assertIn("incremental_download_sessions", code)
                self.assertIn("publish_remote_revision(", code)
                self.assertNotIn(
                    'incoming = generated / "incremental_audio"', code,
                )


if __name__ == "__main__":
    unittest.main()
