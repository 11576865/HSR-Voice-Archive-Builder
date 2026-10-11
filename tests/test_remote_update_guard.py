from __future__ import annotations

import hashlib
import io
import json
import wave
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.project import create_project
from app.remote_update_guard import choose_remote_additions, verify_combined_source_no_collisions, verified_completed_new_wavs
from app.security import api_token
from app.server import app, _clear_active, _set_active


class RemoteIncrementalSafetyTests(unittest.TestCase):
    def setUp(self):
        _clear_active()

    def tearDown(self):
        _clear_active()

    def _files(self, root: Path):
        output = root / "output"
        output.mkdir()
        rows = [{
            "index": 1, "filename": "voice.wav",
            "source_member_id": "chapter_a/voice.wav",
            "source_text": "Original text", "sha256": "a" * 64,
        }]
        manifest = output / "manifest.json"
        manifest.write_text(json.dumps({"entries": rows}), encoding="utf-8")
        index = root / "index.csv"
        index.write_text("filename,source_member_id\nvoice.wav,chapter_a/voice.wav\n", encoding="utf-8")
        plan = {
            "remote_rows": 3,
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "new_logical": [
                {"filename": "new_a.wav", "metadata": {"filename": "new_a.wav", "english": "One"}},
                {"filename": "new_b.wav", "metadata": {"filename": "new_b.wav", "english": "Two"}},
            ],
        }
        return manifest, index, plan

    def test_selected_only_and_legacy_all(self):
        with tempfile.TemporaryDirectory() as td:
            manifest, index, plan = self._files(Path(td))
            selected = choose_remote_additions(
                plan, manifest, index, selected_filenames=["new_b.wav"],
            )
            self.assertEqual([row["filename"] for row in selected], ["new_b.wav"])
            self.assertEqual(len(choose_remote_additions(plan, manifest, index)), 2)

    def test_empty_unknown_duplicate_and_disguised_path_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            manifest, index, plan = self._files(Path(td))
            for names in ([], ["other.wav"], ["new_a.wav", "new_a.wav"],
                          ["../new_a.wav"], ["CHAPTER/new_a.wav"]):
                with self.subTest(names=names), self.assertRaises(ValueError):
                    choose_remote_additions(
                        plan, manifest, index, selected_filenames=names,
                    )

    def test_reject_stale_manifest_even_when_candidate_unmodified(self):
        with tempfile.TemporaryDirectory() as td:
            manifest, index, plan = self._files(Path(td))
            manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Archive changed"):
                choose_remote_additions(plan, manifest, index)

    def test_existing_nested_member_with_identical_basename_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            manifest, index, plan = self._files(Path(td))
            plan["new_logical"].append({
                "filename": "voice.wav", "metadata": {"filename": "voice.wav"},
            })
            with self.assertRaisesRegex(ValueError, "collide"):
                choose_remote_additions(
                    plan, manifest, index, selected_filenames=["voice.wav"],
                )

    def test_conflicting_provider_identity_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            manifest, index, plan = self._files(Path(td))
            plan["new_logical"][0]["metadata"]["filename"] = "other.wav"
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                choose_remote_additions(plan, manifest, index)

    def test_source_merge_preserves_nested_wavs_and_detects_same_basename(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = root / "existing"
            incoming = root / "incoming"
            (original / "chapter_a").mkdir(parents=True)
            incoming.mkdir()
            (original / "chapter_a" / "voice.wav").write_bytes(b"VOICE-ORIGINAL")
            (incoming / "new.wav").write_bytes(b"VOICE-NEW")
            verify_combined_source_no_collisions(original, incoming)
            before = (original / "chapter_a" / "voice.wav").read_bytes()
            (incoming / "voice.wav").write_bytes(b"BAD-REPLACEMENT")
            with self.assertRaisesRegex(ValueError, "collides"):
                verify_combined_source_no_collisions(original, incoming)
            self.assertEqual((original / "chapter_a" / "voice.wav").read_bytes(), before)

    def test_fastapi_and_termux_publish_immutable_audio_versions(self):
        project_root = Path(__file__).resolve().parents[1]
        for backend in ("server.py", "lite_server.py"):
            with self.subTest(backend=backend):
                source = (project_root / "app" / backend).read_text(encoding="utf-8")
                self.assertIn('generated / "combined_audio_versions" / f"combined-{version}"', source)
                self.assertIn('generated / f"quick_index_incremental_{version}.csv"', source)
                self.assertIn("staging.replace(combined)", source)
                self.assertNotIn("shutil.rmtree(combined)", source)
                self.assertIn("choose_remote_additions(", source)

    def test_completed_download_requires_exact_session_file_hash_and_wav_header(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            root.mkdir(exist_ok=True)
            with io.BytesIO() as buf:
                with wave.open(buf, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(b"\x00\x00" * 32)
                content = buf.getvalue()
            wav_path = root / "selected.wav"
            wav_path.write_bytes(content)
            target = [{"filename": "selected.wav"}]
            record = {
                "filename": "selected.wav", "status": "ok",
                "path": str(wav_path),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            self.assertEqual(
                verified_completed_new_wavs(target, {"completed": [record]}, root),
                {"selected.wav"},
            )
            failures = [
                (dict(record, filename="unselected.wav"), "Unexpected completed"),
                (dict(record, filename="../selected.wav"), "non-basename"),
                (dict(record, status="exists"), "Unverified cached"),
                (dict(record, path=str(root / "elsewhere.wav")), "path mismatch"),
                (dict(record, sha256="a" * 64), "checksum mismatch"),
            ]
            for bad, reason in failures:
                with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                    verified_completed_new_wavs(target, {"completed": [bad]}, root)
            with self.assertRaisesRegex(ValueError, "Duplicate completed"):
                verified_completed_new_wavs(target, {"completed": [record, record]}, root)
            wav_path.write_bytes(b"not audio at all")
            updated = dict(record, sha256=hashlib.sha256(wav_path.read_bytes()).hexdigest())
            with self.assertRaisesRegex(ValueError, "not a RIFF/WAVE"):
                verified_completed_new_wavs(target, {"completed": [updated]}, root)
            wav_path.unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                verified_completed_new_wavs(target, {"completed": [record]}, root)

    def test_old_download_cache_is_not_part_of_selected_successful_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original, incoming = root / "original", root / "incoming"
            original.mkdir()
            incoming.mkdir()
            (original / "existing.wav").write_bytes(b"original")
            (incoming / "chosen.wav").write_bytes(b"chosen")
            (incoming / "old-cache.wav").write_bytes(b"left-from-other-run")
            verify_combined_source_no_collisions(
                original, incoming, successful_filenames={"chosen.wav"},
            )
            with self.assertRaisesRegex(ValueError, "missing"):
                verify_combined_source_no_collisions(
                    original, incoming, successful_filenames={"not-downloaded.wav"},
                )
            # Unselected cached WAVs must not block the requested comparison.
            (incoming / "existing.wav").write_bytes(b"stale-conflict")
            verify_combined_source_no_collisions(
                original, incoming, successful_filenames={"chosen.wav"},
            )
            with self.assertRaisesRegex(ValueError, "collides"):
                verify_combined_source_no_collisions(
                    original, incoming, successful_filenames={"existing.wav"},
                )

    def test_api_rejects_legacy_unbound_plan_before_starting_job(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest, index, plan = self._files(root)
            del plan["manifest_sha256"]
            (manifest.parent / "update_plan.json").write_text(json.dumps(plan), encoding="utf-8")
            config = create_project(
                root, name="updated-guard-test", index_csv="index.csv",
                wav_source="wavs", output_dir="output",
            )
            _set_active(config)
            client = TestClient(app, base_url="http://127.0.0.1:8765")
            with patch("app.server.create_job", return_value=SimpleNamespace(id="should-not-run")) as job:
                response = client.post(
                    "/api/update/apply-remote",
                    data={"selected_filenames": json.dumps(["new_a.wav"])},
                    headers={"X-HSR-Token": api_token()},
                )
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("Archive changed", response.json()["error"])
            job.assert_not_called()

    def test_api_selected_files_forwarded_to_job_only_after_validation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest, index, plan = self._files(root)
            (manifest.parent / "update_plan.json").write_text(json.dumps(plan), encoding="utf-8")
            config = create_project(
                root, name="updated-guard-test", index_csv="index.csv",
                wav_source="wavs", output_dir="output",
            )
            _set_active(config)
            client = TestClient(app, base_url="http://127.0.0.1:8765")
            with patch("app.server.create_job", return_value=SimpleNamespace(id="apply-job")) as job:
                response = client.post(
                    "/api/update/apply-remote",
                    data={"selected_filenames": json.dumps(["new_b.wav"])},
                    headers={"X-HSR-Token": api_token()},
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["job"], "apply-job")
            self.assertEqual(job.call_args.args[0], "remote-update-apply")
            job.assert_called_once()


if __name__ == "__main__":
    unittest.main()
