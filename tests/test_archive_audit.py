from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.archive_audit import audit_archive
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
                response = TestClient(app).get(
                    "/api/project/active/archive/audit",
                    headers={"X-HSR-Token": api_token()},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["audit"]["summary"]["entries"], 1)
            finally:
                _clear_active()

    def test_requires_existing_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                audit_archive(Path(td))
