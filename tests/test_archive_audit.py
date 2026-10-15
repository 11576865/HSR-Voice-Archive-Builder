from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.archive_audit import audit_archive, verify_archive_source_audio
from app.project import create_project
from app.security import api_token
from app.server import app, _set_active, _clear_active
from fastapi.testclient import TestClient


class ArchiveAuditTests(unittest.TestCase):
    def test_chapter_coverage_and_same_basename_identity(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            rows = [
                dict(index=1, filename="same.wav", source_member_id="story/a/same.wav",
                     group="主线", source_text="Hello", target_text="你好",
                     sha256="a" * 64, start_seconds=0, audio_end_seconds=1,
                     display_end_seconds=1),
                dict(index=2, filename="same.wav", source_member_id="story/b/same.wav",
                     group="同行", source_text="Thanks", target_text="谢谢",
                     sha256="b" * 64, start_seconds=2, audio_end_seconds=3,
                     display_end_seconds=3),
            ]
            (output / "manifest.json").write_text(
                json.dumps({"entries": rows}), encoding="utf-8",
            )
            result = audit_archive(output)
            self.assertEqual(result["summary"]["entries"], 2)
            self.assertEqual(result["summary"]["groups"], 2)
            self.assertEqual(result["summary"]["duplicate_basename_groups"], 1)
            self.assertEqual(result["summary"]["issues"], 0)
            self.assertEqual([row["name"] for row in result["groups"]], ["主线", "同行"])

    def test_conflicting_member_and_bad_time_reported_without_writes(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            rows = [
                dict(index=1, filename="x.wav", source_member_id="a/x.wav",
                     source_text="a", target_text="a", sha256="a" * 64,
                     start_seconds=5, audio_end_seconds=6, display_end_seconds=6),
                dict(index=1, filename="x.wav", source_member_id="a/x.wav",
                     source_text="", target_text="", sha256="not-a-hash",
                     start_seconds=3, audio_end_seconds=2, display_end_seconds=2),
            ]
            path = output / "manifest.json"
            path.write_text(json.dumps({"entries": rows}), encoding="utf-8")
            before = path.read_bytes()
            result = audit_archive(output)
            codes = {item["code"] for item in result["issues"]}
            self.assertTrue({
                "duplicate_entry_id", "duplicate_source_member",
                "invalid_source_timeline", "source_audio_hash_missing",
            }.issubset(codes))
            self.assertEqual(path.read_bytes(), before)

    def test_fastapi_project_route_reports_the_same_audit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            output.mkdir()
            rows = [dict(index=1, filename="scene.wav",
                         source_member_id="chapter/scene.wav", group="chapter",
                         source_text="Hello", target_text="你好", sha256="a" * 64,
                         start_seconds=0, audio_end_seconds=1, display_end_seconds=1)]
            (output / "manifest.json").write_text(
                json.dumps({"entries": rows}), encoding="utf-8",
            )
            config = create_project(
                root, name="audit-test", index_csv="index.csv",
                wav_source="wavs", output_dir="output",
            )
            _set_active(config)
            try:
                response = TestClient(app, base_url="http://127.0.0.1:8765").get(
                    "/api/project/active/archive/audit",
                    headers={"X-HSR-Token": api_token()},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["audit"]["summary"]["entries"], 1)
            finally:
                _clear_active()

    def test_stream_source_verification_respects_member_identity_and_flags_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "output"
            wavs = root / "wavs"
            output.mkdir()
            rows = []
            for index, chapter, data in (
                (1, "first", b"RIFF-original-one"),
                (2, "second", b"RIFF-original-two"),
            ):
                folder = wavs / chapter
                folder.mkdir(parents=True)
                (folder / "same.wav").write_bytes(data)
                rows.append({
                    "index": index, "filename": "same.wav",
                    "source_member_id": chapter + "/same.wav",
                    "group": chapter, "sha256": hashlib.sha256(data).hexdigest(),
                    "source_text": "Original", "target_text": "翻译",
                    "start_seconds": index, "audio_end_seconds": index + 1,
                    "display_end_seconds": index + 1,
                })
            (output / "manifest.json").write_text(
                json.dumps({"entries": rows}), encoding="utf-8",
            )
            config = create_project(
                root, name="verify-test", index_csv="index.csv",
                wav_source="wavs", output_dir="output",
            )
            progress = []
            initial = verify_archive_source_audio(
                config, output, report_progress=lambda *args: progress.append(args),
            )
            self.assertTrue(initial["summary"]["trusted_complete"])
            self.assertEqual(initial["summary"]["verified"], 2)
            self.assertEqual(initial["summary"]["mismatch"], 0)
            self.assertTrue(progress)
            original_manifest = (output / "manifest.json").read_bytes()
            (wavs / "second" / "same.wav").write_bytes(b"RIFF-changed-same-duration")
            after = verify_archive_source_audio(config, output)
            self.assertFalse(after["summary"]["trusted_complete"])
            self.assertEqual(after["summary"]["verified"], 1)
            self.assertEqual(after["summary"]["mismatch"], 1)
            self.assertEqual(after["findings"][0]["id"], "2")
            self.assertEqual((output / "manifest.json").read_bytes(), original_manifest)

    def test_normalized_wav_manifest_hash_is_inconclusive_not_false_corruption(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output, wavs = root / "output", root / "wavs"
            output.mkdir()
            wavs.mkdir()
            raw = b"RIFF-original-pcm"
            (wavs / "line.wav").write_bytes(raw)
            (output / "manifest.json").write_text(
                json.dumps({
                    "report": {"count_wav_pcm_normalized": 1},
                    "entries": [{
                        "index": 1, "filename": "line.wav",
                        "source_member_id": "line.wav",
                        "sha256": hashlib.sha256(b"RIFF-normalized-pcm").hexdigest(),
                    }],
                }), encoding="utf-8",
            )
            config = create_project(
                root, name="normalization", index_csv="index.csv",
                wav_source="wavs", output_dir="output",
            )
            result = verify_archive_source_audio(config, output)
            self.assertFalse(result["summary"]["trusted_complete"])
            self.assertEqual(result["summary"]["normalization_ambiguous"], 1)
            self.assertEqual(result["summary"]["mismatch"], 0)

    def test_source_verifier_never_chooses_ambiguous_basename(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out, wavs = root / "output", root / "wavs"
            out.mkdir()
            for dirname in ("a", "b"):
                sub = wavs / dirname
                sub.mkdir(parents=True)
                (sub / "same.wav").write_bytes(b"RIFF" + dirname.encode())
            (out / "manifest.json").write_text(
                json.dumps({"entries": [{
                    "index": 1, "filename": "same.wav", "sha256": "a" * 64,
                }]}), encoding="utf-8",
            )
            config = create_project(
                root, name="ambiguous", index_csv="index.csv",
                wav_source="wavs", output_dir="output",
            )
            result = verify_archive_source_audio(config, out)
            self.assertEqual(result["summary"]["ambiguous"], 1)
            self.assertEqual(result["summary"]["verified"], 0)

    def test_fastapi_start_hash_job_reports_existing_project(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "output"
            out.mkdir()
            (out / "manifest.json").write_text('{"entries":[]}', encoding="utf-8")
            config = create_project(
                root, name="job-test", index_csv="index.csv",
                wav_source="wavs", output_dir="output",
            )
            _set_active(config)
            try:
                with patch("app.server.create_job", return_value=SimpleNamespace(id="job-a")) as job:
                    response = TestClient(
                        app, base_url="http://127.0.0.1:8765",
                    ).post(
                        "/api/project/active/archive/verify-sources",
                        headers={"X-HSR-Token": api_token()},
                    )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["job"], "job-a")
                job.assert_called_once()
                self.assertEqual(job.call_args.args[0], "archive-verify")
            finally:
                _clear_active()

    def test_requires_existing_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                audit_archive(Path(td))
